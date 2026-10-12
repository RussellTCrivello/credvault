"""
Durable import engine with checkpointing, recovery, and record-level tracking.

This module provides a persistent import-job lifecycle and a hash-only
per-source-line ledger. It can resume unaccounted positions only when an
unchanged path-backed source can be re-read; uploaded bytes are not retained,
so an interrupted upload may remain partial. Recovery is bounded by the
source manifest and does not claim to cover host/storage failures.

1. Idempotency -- each source position has a deterministic unique key, and
   one transaction commits its account rows, outcomes, counters and checkpoint.
2. Counters -- committed/skipped/failed/attempted totals are refreshed from
   the ledger; discovered counts come from the immutable source manifest when
   one exists.
3. PostgreSQL durability -- durable batches locally set synchronous_commit
   to ON. The guarantee still depends on PostgreSQL WAL/fsync settings and
   storage honoring flush requests.
4. Lease fencing -- only the current owner of a live job-row lease may commit
   a batch or release that lease. Expired leases can be recovered atomically.
"""

import hashlib
import logging
import os
import signal
import threading
import time
import uuid
from typing import NamedTuple

from django.db import connection, models as dj_models, transaction
from django.utils import timezone

from .importer import (
    _REFERENCE_INSERTS, _STAGE_DDL,
    collect_files_from_path, stage_rows, worker_count,
)
from .models import (
    Account, Email, ImportJob, ImportRecord, ImportRecoveryLog,
    Ip, Pass, Url, User, IMPORT_LEASE_SECONDS, record_fingerprint,
)
from .utils import parse_line, NO_IP
from .validators import (
    classify_identifier, normalize_ip, normalize_password, normalize_url,
)

logger = logging.getLogger(__name__)
_lease_context = threading.local()

#: The SQL to insert Account rows from the staging table.
#: Identical to importer._ACCOUNT_INSERT but with RETURNING so we can
#: capture which rows were actually inserted (not duplicates).
_FINGERPRINT_SQL = (
    "encode(sha256(convert_to("
    " coalesce(k.url_id::text, '') || '|' || k.account_type || '|' ||"
    " coalesce(k.email_id::text, '') || '|' ||"
    " coalesce(k.user_id::text, '') || '|' ||"
    " coalesce(k.password_id::text, '') || '|' ||"
    " coalesce(k.ip_id::text, ''), 'UTF8')), 'hex')"
)

_ACCOUNT_INSERT_RETURNING = (
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
    " RETURNING fingerprint, id"
)


# ---------------------------------------------------------------------------
# Worker identity
# ---------------------------------------------------------------------------

def _current_worker_id():
    """Stable identifier for the current process."""
    return f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


class PositionedLine(NamedTuple):
    """
    One source line together with its original 1-based source position.

    Recovery yields these when it resumes a job: the ledger position (and
    therefore the idempotency key) must keep matching the position in the
    source even when earlier lines were already processed, or when ledger
    rows for skipped lines were lost with the crash.  Plain lines (str or
    bytes) are still accepted everywhere and are numbered sequentially.
    """

    position: int
    line: object


# ---------------------------------------------------------------------------
# Lease management
# ---------------------------------------------------------------------------

def acquire_lease(job, worker_id=None):
    """Atomically acquire a lease only for a resumable job state.

    A conditional UPDATE acts as the compare-and-swap: concurrent claimants
    cannot both take an unowned/expired lease, and terminal/cancelled jobs
    cannot be revived merely because their worker id is blank.
    """
    worker_id = worker_id or _current_worker_id()
    now = timezone.now()
    expires = now + timezone.timedelta(seconds=IMPORT_LEASE_SECONDS)
    resumable = (
        dj_models.Q(status__in=(
            'pending', 'paused', 'interrupted', 'failed',
            'completed_with_errors',
        ))
        | (
            dj_models.Q(status='processing')
            & (
                dj_models.Q(lease_expires_at__lte=now)
                | dj_models.Q(lease_expires_at__isnull=True)
                | dj_models.Q(worker_id='')
            )
        )
    )
    unowned = (
        dj_models.Q(worker_id='')
        | dj_models.Q(worker_id__isnull=True)
        | dj_models.Q(lease_expires_at__lte=now)
        | dj_models.Q(lease_expires_at__isnull=True)
    )

    already_owned_by_this_worker = dj_models.Q(
        status='processing',
        worker_id=worker_id,
        lease_expires_at__gt=now,
    )
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute('SET LOCAL synchronous_commit = on')
        updated = ImportJob.objects.filter(pk=job.pk).filter(
            (resumable & unowned) | already_owned_by_this_worker,
        ).update(
            worker_id=worker_id,
            lease_expires_at=expires,
            last_activity_at=now,
        )
    if updated:
        job.worker_id = worker_id
        job.lease_expires_at = expires
        job.last_activity_at = now
        return True
    return False


def renew_lease(job, worker_id=None):
    """Extend only the still-live lease owned by this worker."""
    expected_worker = worker_id or job.worker_id
    if not expected_worker:
        return False
    now = timezone.now()
    expires = now + timezone.timedelta(seconds=IMPORT_LEASE_SECONDS)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute('SET LOCAL synchronous_commit = on')
        updated = ImportJob.objects.filter(
            pk=job.pk,
            worker_id=expected_worker,
            lease_expires_at__gt=now,
            status__in=('pending', 'processing', 'paused', 'cancelled'),
        ).update(
            lease_expires_at=expires,
            last_activity_at=now,
        )
    if updated:
        job.lease_expires_at = expires
        job.last_activity_at = now
    return bool(updated)


def release_lease(job, worker_id=None):
    """Release this worker's lease without clearing a replacement lease."""
    expected_worker = worker_id or job.worker_id
    if not expected_worker:
        return False
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute('SET LOCAL synchronous_commit = on')
        updated = ImportJob.objects.filter(
            pk=job.pk, worker_id=expected_worker,
        ).update(worker_id='', lease_expires_at=None)
    if updated:
        job.worker_id = ''
        job.lease_expires_at = None
    return bool(updated)


# ---------------------------------------------------------------------------
# Import job creation
# ---------------------------------------------------------------------------

def create_import_job(
    source_name,
    source_path='',
    source_size=None,
    source_hash='',
    import_format='auto',
    options=None,
):
    """Create a new ImportJob in the pending state."""
    return ImportJob.objects.create(
        source_name=source_name,
        source_path=source_path,
        source_size=source_size,
        source_hash=source_hash,
        import_format=import_format,
        options=options or {},
        status='pending',
        stage='initializing',
    )


def _hash_source_line(line):
    """SHA-256 of a source line (str or bytes)."""
    if isinstance(line, str):
        line = line.encode('utf-8', errors='replace')
    elif not isinstance(line, (bytes, bytearray)):
        line = str(line).encode('utf-8', errors='replace')
    return hashlib.sha256(line).hexdigest()


def _is_sha256_digest(value):
    """Whether a stored value looks like a lowercase SHA-256 digest."""
    return (
        len(value or '') == 64
        and all(character in '0123456789abcdef' for character in value)
    )


def _hash_file(filepath):
    """Legacy whole-file SHA-256 used to upgrade pre-manifest file jobs."""
    digest = hashlib.sha256()
    try:
        with open(filepath, 'rb') as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b''):
                digest.update(chunk)
    except OSError:
        return ''
    return digest.hexdigest()


def _source_manifest(
    target_path, ext='*', files=None, on_file_scanned=None,
    on_file_signature=None,
):
    """Return (digest, byte_size, line_count, ordered_files) for a source.

    The digest covers normalized relative names, sizes, and file contents,
    so directory membership/order changes are detected as well as edits to
    an individual file. Only hashes and metadata are persisted. ``files``
    can pin the selected file list for a progress scan; a later pre-import
    validation still re-enumerates and verifies directory membership.
    """
    if os.path.isfile(target_path):
        root = os.path.dirname(os.path.abspath(target_path))
        if files is None:
            files = [os.path.abspath(target_path)]
    elif os.path.isdir(target_path):
        root = os.path.abspath(target_path)
        if files is None:
            files = [
                os.path.abspath(filepath)
                for filepath in collect_files_from_path(target_path, ext=ext)
            ]
    else:
        raise SourceChangedError('Import source is missing or unreadable.')

    files = [os.path.abspath(filepath) for filepath in files]
    manifest = hashlib.sha256()
    manifest.update(b'credvault-source-manifest-v1\0')
    total_size = 0
    total_lines = 0
    for filepath in files:
        try:
            relative = os.path.relpath(filepath, root).replace(os.sep, '/')
            file_hash = hashlib.sha256()
            file_size = 0
            newline_count = 0
            last_byte = b''
            with open(filepath, 'rb') as handle:
                expected_signature = _file_stat_signature(
                    os.fstat(handle.fileno()),
                )
                if on_file_signature is not None:
                    on_file_signature(filepath, expected_signature)
                for chunk in iter(lambda: handle.read(1 << 16), b''):
                    file_hash.update(chunk)
                    file_size += len(chunk)
                    newline_count += chunk.count(b'\n')
                    if chunk:
                        last_byte = chunk[-1:]
                _check_streamed_file_stat(
                    handle, filepath, expected_signature, check_path=True,
                )
        except OSError as exc:
            raise SourceChangedError(
                'An import source file is missing or unreadable.'
            ) from exc
        file_lines = newline_count + int(file_size > 0 and last_byte != b'\n')
        manifest.update(relative.encode('utf-8', errors='surrogatepass'))
        manifest.update(b'\0')
        manifest.update(str(file_size).encode('ascii'))
        manifest.update(b':')
        manifest.update(file_hash.digest())
        total_size += file_size
        total_lines += file_lines
        if on_file_scanned is not None:
            on_file_scanned(filepath, file_size, file_lines)
    return manifest.hexdigest(), total_size, total_lines, files


def _validate_source_manifest(
    job, target_path=None, ext=None, on_file_signature=None,
):
    """Validate a path source and optionally capture its file signatures."""
    target_path = target_path or job.source_path
    if not target_path:
        return None
    source_ext = ext or (job.options or {}).get('ext', '*')
    digest, byte_size, line_count, files = _source_manifest(
        target_path,
        ext=source_ext,
        on_file_signature=on_file_signature,
    )
    if job.source_hash:
        if job.source_hash != digest:
            # Prior releases stored the raw file digest rather than the
            # versioned manifest digest. Upgrade only when the current single
            # file exactly matches that legacy fingerprint.
            legacy_match = (
                os.path.isfile(target_path)
                and job.source_hash == _hash_file(target_path)
            )
            if not legacy_match:
                raise SourceChangedError(
                    'Import source does not match the recorded manifest.'
                )
            job.source_hash = digest
            job.source_size = byte_size
            job.save(update_fields=['source_hash', 'source_size'])
        if job.source_size is not None and job.source_size != byte_size:
            raise SourceChangedError(
                'Import source size does not match the recorded manifest.'
            )
        if job.source_size is None:
            job.source_size = byte_size
            job.save(update_fields=['source_size'])
    elif job.records.exists() or job.total_attempted:
        # A legacy job without a manifest cannot safely resume: there is no
        # original identity against which the current path can be compared.
        raise SourceChangedError(
            'Import job has no source manifest; resume is unsafe.'
        )
    else:
        job.source_hash = digest
        job.source_size = byte_size
        job.save(update_fields=['source_hash', 'source_size'])

    if job.total_discovered != line_count:
        # The verified source manifest is authoritative for path-backed jobs;
        # an old counter may reflect only the lines a worker had read before
        # it stopped.
        job.total_discovered = line_count
        job.save(update_fields=['total_discovered'])
    return digest, byte_size, line_count, files


def _file_stat_signature(stat_result):
    """Metadata used to detect in-place edits/replacements during streaming."""
    return (
        getattr(stat_result, 'st_dev', None),
        getattr(stat_result, 'st_ino', None),
        stat_result.st_size,
        getattr(
            stat_result, 'st_mtime_ns',
            int(stat_result.st_mtime * 1_000_000_000),
        ),
        getattr(
            stat_result, 'st_ctime_ns',
            int(stat_result.st_ctime * 1_000_000_000),
        ),
    )


def _check_streamed_file_stat(
    handle, filepath, expected_signature, check_path=False,
):
    """Refuse metadata changes; optionally verify the path still names it."""
    try:
        file_signature = _file_stat_signature(os.fstat(handle.fileno()))
        path_signature = (
            _file_stat_signature(os.stat(filepath)) if check_path else
            expected_signature
        )
    except OSError as exc:
        raise SourceChangedError(
            'An import source file became unavailable while it was read.'
        ) from exc
    if file_signature != expected_signature or path_signature != expected_signature:
        raise SourceChangedError(
            'Import source file changed while it was being read.'
        )


class _VerifiedSourceLines:
    """Stream a path-backed source with integrity checks at commit boundaries.

    Hashing still covers every byte. File metadata is checked when each file
    opens and closes, and by ``check_source`` immediately before every durable
    batch commit and after progress callbacks. This avoids a metadata syscall
    for every line without allowing a changed batch to be committed.
    """

    def __init__(
        self, job, target_path, ext, files, before_stream=None,
        expected_signatures=None,
    ):
        self.job = job
        self.target_path = target_path
        self.ext = ext
        self.files = [os.path.abspath(filepath) for filepath in files]
        self.before_stream = before_stream
        self._expected_signatures = (
            expected_signatures if expected_signatures is not None else {}
        )
        self._active_handle = None
        self._active_filepath = None
        self._active_signature = None
        self._file_signatures = {}
        self._iterator = self._iterate()

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._iterator)

    def close(self):
        try:
            self._iterator.close()
        finally:
            self._active_handle = None
            self._active_filepath = None
            self._active_signature = None

    def check_source(self):
        """Check the active file and all files already read in this stream."""
        active_filepath = self._active_filepath
        if self._active_handle is not None and not self._active_handle.closed:
            _check_streamed_file_stat(
                self._active_handle,
                active_filepath,
                self._active_signature,
                check_path=True,
            )

        for filepath, expected_signature in self._file_signatures.items():
            if filepath == active_filepath:
                continue
            try:
                current_signature = _file_stat_signature(os.stat(filepath))
            except OSError as exc:
                raise SourceChangedError(
                    'An import source file became unavailable while it was read.'
                ) from exc
            if current_signature != expected_signature:
                raise SourceChangedError(
                    'Import source file changed while it was being read.'
                )

    def _iterate(self):
        if self.before_stream is not None:
            validated_files = self.before_stream()
            if validated_files is not None:
                self.files = [
                    os.path.abspath(filepath) for filepath in validated_files
                ]

        absolute_target = os.path.abspath(self.target_path)
        if os.path.isfile(absolute_target):
            root = os.path.dirname(absolute_target)
        elif os.path.isdir(absolute_target):
            root = absolute_target
        else:
            raise SourceChangedError('Import source is no longer available.')

        manifest = hashlib.sha256()
        manifest.update(b'credvault-source-manifest-v1\0')
        total_size = 0
        total_lines = 0
        try:
            for filepath in self.files:
                relative = os.path.relpath(filepath, root).replace(os.sep, '/')
                file_hash = hashlib.sha256()
                file_size = 0
                newline_count = 0
                last_byte = b''
                try:
                    with open(filepath, 'rb') as handle:
                        current_signature = _file_stat_signature(
                            os.fstat(handle.fileno()),
                        )
                        expected_stat = self._expected_signatures.get(
                            filepath,
                        )
                        if (
                            expected_stat is not None
                            and current_signature != expected_stat
                        ):
                            raise SourceChangedError(
                                'Import source file changed before it was read.'
                            )
                        if expected_stat is None:
                            expected_stat = current_signature
                        self._file_signatures[filepath] = expected_stat
                        self._active_handle = handle
                        self._active_filepath = filepath
                        self._active_signature = expected_stat
                        self.check_source()
                        while True:
                            line = handle.readline()
                            if not line:
                                break
                            file_hash.update(line)
                            file_size += len(line)
                            newline_count += line.count(b'\n')
                            if line:
                                last_byte = line[-1:]
                            yield line
                        self.check_source()
                except OSError as exc:
                    raise SourceChangedError(
                        'An import source file became unreadable.'
                    ) from exc
                finally:
                    self._active_handle = None
                    self._active_filepath = None
                    self._active_signature = None

                file_lines = newline_count + int(
                    file_size > 0 and last_byte != b'\n'
                )
                manifest.update(
                    relative.encode('utf-8', errors='surrogatepass')
                )
                manifest.update(b'\0')
                manifest.update(str(file_size).encode('ascii'))
                manifest.update(b':')
                manifest.update(file_hash.digest())
                total_size += file_size
                total_lines += file_lines

            for filepath, expected_signature in self._file_signatures.items():
                try:
                    current_signature = _file_stat_signature(os.stat(filepath))
                except OSError as exc:
                    raise SourceChangedError(
                        'An import source file became unavailable while it was read.'
                    ) from exc
                if current_signature != expected_signature:
                    raise SourceChangedError(
                        'Import source file changed while it was being read.'
                    )

            if (
                manifest.hexdigest() != self.job.source_hash
                or total_size != self.job.source_size
                or total_lines != self.job.total_discovered
            ):
                raise SourceChangedError(
                    'Import source changed while it was being read.'
                )
            current_files = (
                [absolute_target]
                if os.path.isfile(absolute_target)
                else [
                    os.path.abspath(filepath)
                    for filepath in collect_files_from_path(
                        absolute_target, ext=self.ext,
                    )
                ]
            )
            if current_files != self.files:
                raise SourceChangedError(
                    'Import source file membership changed while it was being read.'
                )
        finally:
            self._active_handle = None
            self._active_filepath = None
            self._active_signature = None


def _iter_source_lines(
    job, target_path, ext, files, before_stream=None,
    expected_signatures=None,
):
    """Return a batch-verified, hash-checking path line iterator."""
    return _VerifiedSourceLines(
        job,
        target_path,
        ext,
        files,
        before_stream=before_stream,
        expected_signatures=expected_signatures,
    )


def _row_from_parsed(record_data):
    """
    Convert a canonical record dict to the 5-tuple staged in the temp table.
    Returns (url, account_type, ident, secret, ip_or_None).
    """
    ip_value = record_data['ip_address']
    if not ip_value or ip_value == NO_IP:
        ip_value = None
    return (
        record_data['url'],
        record_data['account_type'],
        record_data['account_identifier'],
        record_data['secret_data'],
        ip_value,
    )


# ---------------------------------------------------------------------------
# Canonical record validation (same logic as importer.canonical_record
# but self-contained so we don't depend on importer internals).
# ---------------------------------------------------------------------------

def _canonical_record(parsed):
    """
    Validate one parsed-line dict and return it with canonical values.
    Returns None when any field doesn't match its format.
    """
    if not parsed:
        return None
    url = normalize_url(parsed.get('url'))
    password = normalize_password(parsed.get('secret_data'))
    account_type, identifier = classify_identifier(
        parsed.get('account_identifier')
    )
    ip_value = normalize_ip(parsed.get('ip_address'))
    if not (url and password and identifier and account_type):
        return None
    return {
        'url': url,
        'account_type': account_type,
        'account_identifier': identifier,
        'secret_data': password,
        'ip_address': ip_value,
    }


# ---------------------------------------------------------------------------
# Durable batch flush
# ---------------------------------------------------------------------------

class SourceChangedError(ValueError):
    """The source no longer matches the immutable manifest for this job."""


class LeaseLostError(RuntimeError):
    """This process no longer owns the import job's live lease."""


def _durable_flush_batch(
    rows, record_metas, job_pk, *, outcome_metas=None, worker_id=None,
):
    """Atomically commit a bounded source batch, all outcomes and counters.

    Valid account rows, invalid/skipped rows, ledger outcomes, counters, and
    the contiguous durable checkpoint share one PostgreSQL transaction. A
    lost acknowledgement therefore leaves either the old checkpoint or the
    complete new batch, never accounts without corresponding provenance.

    ``record_metas`` is parallel to ``rows``. ``outcome_metas`` carries
    already-decided skipped/failed outcomes that have no staged account row.
    The raw source line is intentionally never written to the ledger because
    it contains the imported credential itself.
    """
    worker_id = worker_id or getattr(_lease_context, 'worker_id', None)
    outcome_metas = list(outcome_metas or ())
    if len(rows) != len(record_metas):
        raise ValueError('rows and record_metas must have the same length')
    if not rows and not outcome_metas:
        return 0

    now = timezone.now()
    record_specs = []
    update_records = []
    new_records = []

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute('SET LOCAL synchronous_commit = on')
        job = ImportJob.objects.select_for_update().get(pk=job_pk)
        if worker_id:
            lease_is_live = (
                job.lease_expires_at is not None
                and job.lease_expires_at > now
            )
            if (
                job.worker_id != worker_id
                or not lease_is_live
                or job.status not in (
                    'pending', 'processing', 'paused', 'cancelled',
                )
            ):
                raise LeaseLostError('Import worker no longer owns this job.')

        incoming = list(zip(rows, record_metas)) + [
            (None, meta) for meta in outcome_metas
        ]
        keys = [meta['idempotency_key'] for _row, meta in incoming]
        existing_by_key = {
            record.idempotency_key: record
            for record in ImportRecord.objects.select_for_update().filter(
                idempotency_key__in=keys,
            )
        } if keys else {}
        seen_in_batch = {}
        candidate_rows = []
        candidate_metas = []
        candidate_existing = []
        candidate_outcomes = []

        for row, meta in incoming:
            key = meta['idempotency_key']
            old_hash = seen_in_batch.get(key)
            if old_hash is not None:
                if old_hash != meta['source_hash']:
                    raise SourceChangedError(
                        'One source position produced conflicting line hashes.'
                    )
                continue
            seen_in_batch[key] = meta['source_hash']

            existing = existing_by_key.get(key)
            if existing is not None:
                if existing.source_hash != meta['source_hash']:
                    raise SourceChangedError(
                        'Source content changed at an already-accounted position.'
                    )
                can_retry = (
                    existing.status in ('pending', 'processing')
                    or (existing.status == 'failed' and existing.retryable)
                )
                if not can_retry:
                    # A repeated call for a terminal line is idempotent.
                    continue

            if row is None:
                candidate_outcomes.append((meta, existing))
            else:
                candidate_rows.append(row)
                candidate_metas.append(meta)
                candidate_existing.append(existing)

        newly_created = {}
        if candidate_rows:
            with connection.cursor() as cursor:
                # Keep PostgreSQL's synchronous_commit default (ON) for the
                # durable path; only the legacy bulk path opts out.
                cursor.execute(_STAGE_DDL)
                cursor.execute('TRUNCATE vault_import_stage')
                with cursor.copy(
                    'COPY vault_import_stage (url, account_type, ident, secret, ip)'
                    ' FROM STDIN'
                ) as copy_ctx:
                    for row in candidate_rows:
                        copy_ctx.write_row(row)
                cursor.execute('ANALYZE vault_import_stage')
                for _table, sql in _REFERENCE_INSERTS:
                    cursor.execute(sql, (now, now))
                cursor.execute(_ACCOUNT_INSERT_RETURNING, (now, now))
                newly_created = {
                    fp: acc_id for fp, acc_id in cursor.fetchall()
                }

            valid_records = _write_ledger_entries(
                candidate_rows, candidate_metas, job_pk, newly_created, now,
            )
            record_specs.extend(zip(valid_records, candidate_existing))

        for meta, existing in candidate_outcomes:
            record_specs.append((ImportRecord(
                import_job_id=job_pk,
                source_position=meta['source_position'],
                source_hash=meta['source_hash'],
                source_line='',
                status=meta['status'],
                error_category=meta.get('error_category', ''),
                error_message=meta.get('error_message', ''),
                retryable=bool(meta.get('retryable', False)),
                idempotency_key=meta['idempotency_key'],
                processed_at=now,
            ), existing))

        deltas = {'committed': 0, 'skipped': 0, 'failed': 0}
        attempted_delta = 0
        committed_delta = 0
        for record, existing in record_specs:
            old_status = existing.status if existing is not None else None
            if old_status in deltas:
                deltas[old_status] -= 1
            elif old_status in (None, 'pending', 'processing'):
                attempted_delta += 1
            else:
                raise ValueError('Unsupported prior ledger status.')
            if record.status in deltas:
                deltas[record.status] += 1
            if record.status == 'committed' and old_status != 'committed':
                committed_delta += 1

            if existing is None:
                new_records.append(record)
            else:
                for field in (
                    'status', 'account_id', 'account_fingerprint',
                    'error_message', 'error_category', 'retryable',
                    'processed_at',
                ):
                    setattr(existing, field, getattr(record, field))
                update_records.append(existing)

        if new_records:
            ImportRecord.objects.bulk_create(new_records)
        if update_records:
            ImportRecord.objects.bulk_update(
                update_records,
                fields=[
                    'status', 'account', 'account_fingerprint',
                    'error_message', 'error_category', 'retryable',
                    'processed_at',
                ],
            )

        max_position = max(
            (meta['source_position'] for _row, meta in incoming),
            default=0,
        )
        if job.source_hash and max_position > job.total_discovered:
            raise SourceChangedError(
                'Source line position exceeds the verified manifest.'
            )
        job.total_discovered = max(job.total_discovered, max_position)
        attempted_before = job.total_attempted
        job.total_attempted = max(0, job.total_attempted + attempted_delta)
        job.total_committed = max(0, job.total_committed + deltas['committed'])
        job.total_skipped = max(0, job.total_skipped + deltas['skipped'])
        job.total_failed = max(0, job.total_failed + deltas['failed'])

        # Advance only across a gap-free durable prefix, never to the last
        # line merely read into memory. Pending rows do not advance it.
        checkpoint = int((job.last_checkpoint or {}).get('accounted_through', 0))
        expected = checkpoint + 1
        batch_positions = {
            meta['source_position'] for _row, meta in incoming
        }
        terminal_statuses = {'committed', 'skipped', 'failed'}
        contiguous_batch = (
            bool(batch_positions)
            and min(batch_positions) == expected
            and max(batch_positions) - min(batch_positions) + 1
            == len(batch_positions)
            and all(
                record.status in terminal_statuses
                for record, _existing in record_specs
            )
        )
        if contiguous_batch and attempted_before == checkpoint:
            # In the common forward-stream case, counters prove there are no
            # previously-accounted positions beyond this contiguous prefix.
            # Avoid rescanning every new ledger row after each durable batch.
            expected = max(batch_positions) + 1
        else:
            # Recovery and out-of-order positioned input still need the full
            # gap scan: terminal rows beyond a missing position do not advance
            # the durable checkpoint.
            position_rows = (
                ImportRecord.objects.filter(
                    import_job_id=job_pk,
                    status__in=('committed', 'skipped', 'failed'),
                    source_position__gte=expected,
                )
                .order_by()
                .values_list('source_position', flat=True)
                .distinct()
                .order_by('source_position')
            )
            for position in position_rows.iterator(chunk_size=1000):
                if position < expected:
                    continue
                if position > expected:
                    break
                expected += 1
        job.last_checkpoint = {
            'accounted_through': expected - 1,
            'updated_at': now.isoformat(),
        }
        job.last_activity_at = now
        update_fields = [
            'total_discovered', 'total_attempted', 'total_committed',
            'total_skipped', 'total_failed', 'last_checkpoint',
            'last_activity_at',
        ]
        if worker_id:
            job.lease_expires_at = now + timezone.timedelta(
                seconds=IMPORT_LEASE_SECONDS,
            )
            update_fields.append('lease_expires_at')
        job.save(update_fields=update_fields)

    return committed_delta


def _write_ledger_entries(rows, record_metas, job_pk, newly_created, now):
    """Return a privacy-preserving ledger outcome for each valid source row."""
    unique_urls = {row[0] for row in rows}
    unique_passwords = {row[3] for row in rows}
    unique_emails = {row[2] for row in rows if row[1] == 'email' and row[2]}
    unique_users = {row[2] for row in rows if row[1] != 'email' and row[2]}
    unique_ips = {row[4] for row in rows if row[4]}

    url_ids = dict(
        Url.objects.filter(name__in=unique_urls).values_list('name', 'id')
    ) if unique_urls else {}
    pass_ids = dict(
        Pass.objects.filter(name__in=unique_passwords).values_list('name', 'id')
    ) if unique_passwords else {}
    email_ids = dict(
        Email.objects.filter(name__in=unique_emails).values_list('name', 'id')
    ) if unique_emails else {}
    user_ids = dict(
        User.objects.filter(name__in=unique_users).values_list('name', 'id')
    ) if unique_users else {}
    ip_ids = dict(
        Ip.objects.filter(name__in=unique_ips).values_list('name', 'id')
    ) if unique_ips else {}

    fingerprints = []
    for row in rows:
        url_id = url_ids.get(row[0])
        password_id = pass_ids.get(row[3])
        email_id = email_ids.get(row[2]) if row[1] == 'email' else None
        user_id = user_ids.get(row[2]) if row[1] != 'email' else None
        ip_id = ip_ids.get(row[4]) if row[4] else None
        if not url_id or not password_id:
            fingerprints.append(None)
        elif row[1] == 'email' and not email_id:
            fingerprints.append(None)
        elif row[1] != 'email' and not user_id:
            fingerprints.append(None)
        else:
            fingerprints.append(record_fingerprint(
                url_id, row[1], email_id, user_id, password_id, ip_id,
            ))

    existing_fingerprints = {
        fingerprint for fingerprint in fingerprints
        if fingerprint is not None and fingerprint not in newly_created
    }
    existing_accounts = {}
    lookup_values = tuple(existing_fingerprints)
    # Stay well below PostgreSQL's bind-parameter limit when a large batch
    # contains many records that already exist in the vault.
    for start in range(0, len(lookup_values), 8000):
        existing_accounts.update(dict(
            Account.objects.filter(
                fingerprint__in=lookup_values[start:start + 8000],
            ).values_list('fingerprint', 'id')
        ))

    records = []
    first_new_record_for_fingerprint = set()
    for meta, fingerprint in zip(record_metas, fingerprints):
        account_id = None
        status = 'failed'
        category = 'missing_reference'
        if fingerprint is not None:
            if (
                fingerprint in newly_created
                and fingerprint not in first_new_record_for_fingerprint
            ):
                status = 'committed'
                account_id = newly_created[fingerprint]
                first_new_record_for_fingerprint.add(fingerprint)
                category = ''
            else:
                account_id = newly_created.get(fingerprint)
                if account_id is None:
                    account_id = existing_accounts.get(fingerprint)
                if account_id is not None:
                    status = 'skipped'
                    category = 'duplicate_fingerprint'

        records.append(ImportRecord(
            import_job_id=job_pk,
            source_position=meta['source_position'],
            source_hash=meta['source_hash'],
            source_line='',
            status=status,
            account_id=account_id,
            account_fingerprint=fingerprint or '',
            error_category=category,
            retryable=False,
            idempotency_key=meta['idempotency_key'],
            processed_at=now,
        ))
    return records


# ---------------------------------------------------------------------------
# Durable import session
# ---------------------------------------------------------------------------

class DurableImportSession:
    """
    Import session that tracks every source record in the database.

    Writes ImportRecord rows for each source line and commits them
    alongside Account rows in atomic batches.  Survives interruptions.
    """

    def __init__(self, job, batch_rows=None, workers=None,
                 on_progress=None, cancel_check=None, worker_id=None,
                 source_check=None, source_close=None):
        self.job = job
        self._batch_rows = batch_rows or stage_rows()
        self._workers = workers if workers is not None else worker_count()
        self._on_progress = on_progress
        self._cancel_check = cancel_check
        self._source_check = source_check
        self._source_close = source_close
        self._position = 0
        self._interrupted = False
        self._paused = False
        self._cancelled = False
        self._worker_id = worker_id or _current_worker_id()
        self._original_sigterm = None
        self._original_sigint = None

    def _install_signal_handlers(self):
        """Install SIGTERM/SIGINT handlers for graceful shutdown."""
        if threading.current_thread() is not threading.main_thread():
            return
        try:
            self._original_sigterm = signal.getsignal(signal.SIGTERM)
            self._original_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGTERM, self._handle_signal)
            signal.signal(signal.SIGINT, self._handle_signal)
        except (OSError, ValueError):
            pass

    def _restore_signal_handlers(self):
        if self._original_sigterm is not None:
            try:
                signal.signal(signal.SIGTERM, self._original_sigterm)
                signal.signal(signal.SIGINT, self._original_sigint)
            except (OSError, ValueError):
                pass

    def _handle_signal(self, signum, frame):
        self._interrupted = True

    def _should_stop(self, flush_batch=None):
        """Poll durable control state and wait in place while paused."""
        if self._interrupted or self._cancelled:
            return True
        if self._cancel_check and self._cancel_check():
            self._cancelled = True
            return True

        try:
            self.job.refresh_from_db(fields=[
                'status', 'worker_id', 'lease_expires_at',
            ])
        except Exception:
            # A real database failure will surface on the next durable batch;
            # do not turn a transient status-read failure into data loss.
            return False

        if self.job.status == 'cancelled':
            self._cancelled = True
            return True
        if self.job.status == 'paused':
            self._paused = True
            if flush_batch:
                flush_batch()
            renew_at = time.monotonic()
            while True:
                if self._interrupted:
                    return True
                if self._cancel_check and self._cancel_check():
                    self._cancelled = True
                    return True
                now = time.monotonic()
                if now >= renew_at:
                    if not renew_lease(self.job, self._worker_id):
                        self._lease_lost = True
                        return True
                    renew_at = now + max(1, IMPORT_LEASE_SECONDS // 3)
                time.sleep(0.25)
                try:
                    self.job.refresh_from_db(fields=[
                        'status', 'worker_id', 'lease_expires_at',
                    ])
                except Exception:
                    continue
                if self.job.status == 'cancelled':
                    self._cancelled = True
                    return True
                if self.job.status == 'paused':
                    continue
                if (
                    self.job.status != 'processing'
                    or self.job.worker_id != self._worker_id
                    or self.job.lease_expires_at is None
                    or self.job.lease_expires_at <= timezone.now()
                ):
                    self._lease_lost = True
                    return True
                self._paused = False
                return False

        if (
            self.job.status != 'processing'
            or self.job.worker_id != self._worker_id
            or self.job.lease_expires_at is None
            or self.job.lease_expires_at <= timezone.now()
        ):
            self._lease_lost = True
            return True
        return False

    def run(self, lines_iterable):
        """Process all lines with durable tracking."""
        return self._run_inner(lines_iterable)

    def _run_inner(self, lines_iterable):
        job = self.job
        source_check = self._source_check or getattr(
            lines_iterable, 'check_source', None,
        )
        source_close = self._source_close or getattr(
            lines_iterable, 'close', None,
        )

        def close_source():
            if callable(source_close):
                try:
                    source_close()
                except Exception:
                    pass

        # Do not change a job's status when another worker owns it, or when
        # it is terminal/cancelled and therefore not resumable.
        if not acquire_lease(job, self._worker_id):
            close_source()
            return job

        if not job.mark_started(worker_id=self._worker_id):
            release_lease(job, self._worker_id)
            close_source()
            return job
        _lease_context.worker_id = self._worker_id
        self._lease_lost = False
        self._install_signal_handlers()
        batch_rows = []
        batch_metas = []
        batch_limit = max(1, int(self._batch_rows))
        batch_valid_count = 0
        batch_outcome_count = 0
        lease_counter = 0
        control_poll_counter = 128

        def flush_batch():
            nonlocal batch_valid_count, batch_outcome_count
            if not batch_metas:
                return
            # ``None`` rows represent source lines which were deliberately
            # skipped or failed; all outcomes use the same transaction as
            # valid account rows and job counters. Validate path-backed input
            # immediately before the transaction so any edited batch remains
            # in memory and cannot be committed under the old manifest.
            if callable(source_check):
                source_check()
            _durable_flush_batch(batch_rows, batch_metas, job.pk)
            batch_rows.clear()
            batch_metas.clear()
            batch_valid_count = 0
            batch_outcome_count = 0
            if self._on_progress:
                self._on_progress(self._position)
                # A callback (or concurrent editor) can change the path after
                # a commit. Refuse before the iterator yields another line.
                if callable(source_check):
                    source_check()

        try:
            for item in lines_iterable:
                if control_poll_counter >= 128:
                    if self._should_stop(flush_batch=flush_batch):
                        flush_batch()
                        break
                    control_poll_counter = 0

                if isinstance(item, PositionedLine):
                    if item.position < 1:
                        raise ValueError('Source positions must start at one.')
                    self._position = item.position
                    line = item.line
                else:
                    self._position += 1
                    line = item

                line_hash = _hash_source_line(line)
                idem_key = ImportRecord.compute_idempotency_key(
                    job.job_id, self._position,
                )
                meta = {
                    'source_position': self._position,
                    'source_hash': line_hash,
                    'idempotency_key': idem_key,
                }
                try:
                    record_data = _canonical_record(parse_line(line))
                except Exception as parse_exc:
                    # Exception text may embed the raw credential line.
                    # Record only the exception class, never its message.
                    batch_rows.append(None)
                    batch_metas.append({
                        **meta,
                        'status': 'failed',
                        'error_category': 'parse_error',
                        'error_message': (
                            'Line parser raised '
                            f'{type(parse_exc).__name__}.'
                        ),
                        'retryable': True,
                    })
                    batch_outcome_count += 1
                else:
                    if record_data is None:
                        batch_rows.append(None)
                        batch_metas.append({
                            **meta,
                            'status': 'skipped',
                            'error_category': 'invalid_format',
                            'retryable': False,
                        })
                        batch_outcome_count += 1
                    else:
                        batch_rows.append(_row_from_parsed(record_data))
                        batch_metas.append(meta)
                        batch_valid_count += 1

                control_poll_counter += 1
                if (
                    batch_valid_count >= batch_limit
                    or batch_outcome_count >= batch_limit
                ):
                    flush_batch()
                    # Recheck immediately after each durable commit so a
                    # pause/cancel request takes effect at the next boundary.
                    control_poll_counter = 128

                lease_counter += 1
                if lease_counter >= 1000:
                    lease_counter = 0
                    if not renew_lease(job, self._worker_id):
                        self._lease_lost = True
                        raise LeaseLostError(
                            'Import worker no longer owns this job.'
                        )

            # Catch a late pause/cancel even when it arrives after the final
            # line but before the worker commits its terminal state.
            self._should_stop(flush_batch=flush_batch)
            if not self._lease_lost:
                flush_batch()

            # Derive the final state after all complete transactions. A stop
            # request takes effect at a batch boundary; unflushed lines are
            # never claimed as processed.
            if self._lease_lost:
                return job
            if self._cancelled:
                job.mark_cancelled(worker_id=self._worker_id)
            elif self._interrupted:
                job.mark_interrupted(worker_id=self._worker_id)
            elif self._paused:
                job.mark_paused(worker_id=self._worker_id)
            else:
                job.refresh_counters()
                if job.total_attempted < job.total_discovered:
                    job.mark_failed(
                        'Source ended before every manifest position was accounted for.',
                        worker_id=self._worker_id,
                    )
                else:
                    job.mark_completed(
                        has_errors=job.total_failed > 0,
                        worker_id=self._worker_id,
                    )
            release_lease(job, self._worker_id)

        except LeaseLostError:
            # Another worker may already own the replacement lease. Do not
            # mark its job failed or clear its lease.
            logger.warning('Import worker lease lost for job %s.', job.job_id)
        except SourceChangedError as exc:
            logger.warning(
                'Import source changed for job %s (%s).',
                job.job_id, type(exc).__name__,
            )
            try:
                job.mark_failed(
                    'Import source changed; resume refused.',
                    worker_id=self._worker_id,
                )
            finally:
                release_lease(job, self._worker_id)
        except Exception as exc:
            # Exception messages and tracebacks can contain source values or
            # database parameters. Keep operational logs and the job record
            # free of imported credentials.
            logger.error(
                'Import job %s failed (%s).', job.job_id,
                type(exc).__name__,
            )
            try:
                job.mark_failed(
                    f'Import session failed ({type(exc).__name__}).',
                    worker_id=self._worker_id,
                )
                release_lease(job, self._worker_id)
            except Exception:
                pass
        finally:
            close_source()
            if getattr(_lease_context, 'worker_id', None) == self._worker_id:
                del _lease_context.worker_id
            self._restore_signal_handlers()

        return job

# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def discover_interrupted_jobs():
    """Find interrupted jobs and pending/processing jobs without a live owner."""
    now = timezone.now()
    unowned_or_expired = (
        dj_models.Q(worker_id='')
        | dj_models.Q(worker_id__isnull=True)
        | dj_models.Q(lease_expires_at__isnull=True)
        | dj_models.Q(lease_expires_at__lte=now)
    )
    return ImportJob.objects.filter(
        dj_models.Q(status='interrupted')
        | (
            dj_models.Q(status='pending')
            & unowned_or_expired
        )
        | (
            dj_models.Q(status='processing')
            & unowned_or_expired
        )
    )


def recover_job(job, lines_iterable_factory=None):
    """Reconcile a job and resume only from a validated durable source.

    The job-row lease fences this recovery from live import workers. Terminal
    ledger outcomes are recounted from the ledger; gaps, retryable failures,
    changed sources, and missing accounts are never reported as success.
    """
    with transaction.atomic():
        job = ImportJob.objects.select_for_update().get(pk=job.pk)
        current_status = job.status
        job.recovery_attempts += 1
        attempt = job.recovery_attempts
        job.save(update_fields=['recovery_attempts'])
        recovery_started = timezone.now()
        log = ImportRecoveryLog.objects.create(
            import_job=job,
            attempt_number=attempt,
            recovery_started_at=recovery_started,
            outcome='running',
        )
        ImportRecoveryLog.objects.filter(
            import_job=job,
            outcome='running',
            recovery_started_at__lt=recovery_started - timezone.timedelta(
                seconds=IMPORT_LEASE_SECONDS,
            ),
        ).exclude(pk=log.pk).update(
            outcome='partial',
            notes='A previous recovery ended before recording a final outcome.',
            recovery_completed_at=recovery_started,
        )

    terminal_job = current_status in ('completed', 'cancelled')
    recovery_worker = _current_worker_id()
    owns_lease = False
    if not terminal_job:
        owns_lease = acquire_lease(job, recovery_worker)
        if not owns_lease:
            log.outcome = 'partial'
            log.notes = 'Recovery was not started: another live lease or terminal state owns the job.'
            log.recovery_completed_at = timezone.now()
            log.save(update_fields=[
                'outcome', 'notes', 'recovery_completed_at',
            ])
            return log
        recovery_started_at = timezone.now()
        started = ImportJob.objects.filter(
            pk=job.pk,
            worker_id=recovery_worker,
            lease_expires_at__gt=recovery_started_at,
            status__in=(
                'pending', 'processing', 'paused', 'interrupted',
                'failed', 'completed_with_errors',
            ),
        ).update(status='processing', stage='recovering')
        if not started:
            job.refresh_from_db(fields=[
                'status', 'stage', 'worker_id', 'lease_expires_at',
            ])
            log.outcome = 'partial'
            log.notes = 'Recovery lost its lease or the job was cancelled before starting.'
            log.recovery_completed_at = timezone.now()
            log.save(update_fields=[
                'outcome', 'notes', 'recovery_completed_at',
            ])
            release_lease(job, recovery_worker)
            return log
        job.status = 'processing'
        job.stage = 'recovering'
        _lease_context.worker_id = recovery_worker

    try:
        available_lines = None
        if (
            lines_iterable_factory is not None
            and not terminal_job
            and not job.records.exists()
        ):
            # A brand-new/legacy empty path job may not yet have a recorded
            # line count; manifest validation can safely establish it before
            # deciding whether any source positions remain.
            available_lines = lines_iterable_factory()
            # Path factories may establish the first immutable manifest on an
            # empty legacy job. They persist those fields via their captured
            # instance, so refresh the locked recovery instance before counting
            # unresolved positions.
            job.refresh_from_db(fields=[
                'source_hash', 'source_size', 'total_discovered',
            ])
        committed_recs = job.records.filter(status='committed')
        previously_committed = committed_recs.count()
        missing_count = 0
        orphaned_account_ids = []
        source_can_retry = lines_iterable_factory is not None

        for rec in committed_recs.iterator(chunk_size=1000):
            current_fingerprint = None
            if rec.account_id:
                current_fingerprint = Account.objects.filter(
                    pk=rec.account_id,
                ).values_list('fingerprint', flat=True).first()
            missing = (
                (rec.account_id is not None and current_fingerprint is None)
                or (
                    rec.account_id is None
                    and (
                        bool(rec.account_fingerprint)
                        or _is_sha256_digest(rec.source_hash)
                    )
                )
                or (
                    current_fingerprint is not None
                    and rec.account_fingerprint
                    and current_fingerprint != rec.account_fingerprint
                )
            )
            if missing:
                if rec.account_id is not None:
                    orphaned_account_ids.append(rec.account_id)
                rec.status = 'failed'
                rec.error_message = 'Committed account failed integrity reconciliation.'
                rec.error_category = 'reconciliation_missing'
                rec.retryable = source_can_retry
                rec.save(update_fields=[
                    'status', 'error_message', 'error_category', 'retryable',
                ])
                missing_count += 1

        job.refresh_counters()
        pending_count = job.records.filter(
            status__in=('pending', 'processing'),
        ).count()
        failed_count = job.records.filter(status='failed').count()
        retryable_failed = job.records.filter(
            status='failed', retryable=True,
        ).count()
        actual_committed = job.records.filter(status='committed').count()
        skipped_count = job.records.filter(status='skipped').count()
        total_records = job.records.count()

        log.records_committed = actual_committed
        log.records_pending = pending_count
        log.records_failed = failed_count
        log.reconciliation_details = {
            'missing_accounts': missing_count,
            'previously_committed': previously_committed,
            'orphaned_account_ids': orphaned_account_ids[:100],
            'total_ledger_records': total_records,
            'manifest_lines': job.total_discovered,
            'retryable_failed': retryable_failed,
        }
        log.save(update_fields=[
            'records_committed', 'records_pending', 'records_failed',
            'reconciliation_details',
        ])

        terminal_positions = set(
            job.records.filter(
                status__in=('committed', 'skipped'),
            ).values_list('source_position', flat=True)
        )
        terminal_positions.update(
            job.records.filter(
                status='failed', retryable=False,
            ).values_list('source_position', flat=True)
        )
        unresolved_count = max(
            0, job.total_discovered - len(terminal_positions),
        )

        if lines_iterable_factory is not None and (unresolved_count or retryable_failed):
            # The factory validates the whole immutable source before it
            # yields, and its generator validates again when exhausted.
            if available_lines is None:
                available_lines = lines_iterable_factory()
            if available_lines is None:
                raise SourceChangedError(
                    'No re-readable source is available for recovery.'
                )
            remaining = (
                PositionedLine(position, line)
                for position, line in enumerate(available_lines, start=1)
                if position not in terminal_positions
            )
            log.outcome = 'partial'
            log.notes = (
                f'Resumed {unresolved_count + retryable_failed} unresolved '
                'source line(s); earlier committed outcomes were retained.'
            )
            log.save(update_fields=['outcome', 'notes'])
            session = DurableImportSession(
                job,
                worker_id=recovery_worker,
                source_check=getattr(available_lines, 'check_source', None),
                source_close=getattr(available_lines, 'close', None),
            )
            session._position = max(
                job.records.values_list('source_position', flat=True),
                default=0,
            )
            session.run(remaining)
            job.refresh_from_db()
            job.refresh_counters()
            if job.status == 'cancelled':
                log.notes = (
                    'Recovery was cancelled before all unresolved source '
                    'lines were processed.'
                )
            log.records_committed = job.total_committed
            log.records_pending = job.records.filter(
                status__in=('pending', 'processing'),
            ).count()
            log.records_failed = job.total_failed
            if job.status == 'completed' and job.total_failed == 0:
                # Keep 'partial': recovery did resume missing lines, even
                # though the resulting job is now complete.
                pass
            elif job.status == 'completed_with_errors':
                log.outcome = 'partial'
            else:
                log.outcome = 'partial'
            log.reconciliation_details = {
                **(log.reconciliation_details or {}),
                'final_discovered': job.total_discovered,
                'final_attempted': job.total_attempted,
                'final_committed': job.total_committed,
                'final_skipped': job.total_skipped,
                'final_failed': job.total_failed,
            }
            log.recovery_completed_at = timezone.now()
            log.save()
            return log

        if pending_count or unresolved_count or retryable_failed:
            log.outcome = 'partial'
            log.notes = (
                f'{pending_count + unresolved_count + retryable_failed} '
                'line(s) remain unaccounted; no verified source is available '
                'for automatic resume.'
            )
            if not terminal_job:
                job.mark_paused(worker_id=recovery_worker)
        elif failed_count or missing_count or current_status == 'completed_with_errors':
            log.outcome = 'partial'
            log.notes = (
                f'Reconciliation found {failed_count} failed ledger row(s); '
                'the job is not reported as a clean success.'
            )
            if current_status != 'cancelled':
                job.mark_completed(
                    has_errors=True, worker_id=recovery_worker,
                )
        elif current_status == 'cancelled':
            log.outcome = 'partial'
            log.notes = 'Cancelled jobs are not resumed by recovery.'
        else:
            log.outcome = 'success'
            log.notes = 'Reconciliation complete; all manifest lines have terminal outcomes.'
            if not terminal_job and not job.mark_completed(
                has_errors=False, worker_id=recovery_worker,
            ):
                log.outcome = 'partial'
                log.notes = 'Recovery could not finalize because its lease was lost.'

        job.refresh_from_db(fields=['status'])
        if job.status == 'cancelled':
            log.outcome = 'partial'
            log.notes = 'Recovery was cancelled before reconciliation finished.'
        log.recovery_completed_at = timezone.now()
        log.save(update_fields=[
            'outcome', 'notes', 'records_committed', 'records_pending',
            'records_failed', 'reconciliation_details',
            'recovery_completed_at',
        ])
    except Exception as exc:
        # Never persist/log exception text from parsers, SQL, or source rows.
        logger.error(
            'Recovery of job %s failed (%s).',
            job.job_id, type(exc).__name__,
        )
        log.outcome = 'failed'
        log.notes = f'Recovery failed ({type(exc).__name__}).'
        log.recovery_completed_at = timezone.now()
        log.save(update_fields=[
            'outcome', 'notes', 'recovery_completed_at',
        ])
        if not terminal_job:
            try:
                job.mark_failed(
                    f'Recovery failed ({type(exc).__name__}).',
                    worker_id=recovery_worker,
                )
            except Exception:
                pass
    finally:
        if owns_lease:
            release_lease(job, recovery_worker)
        if getattr(_lease_context, 'worker_id', None) == recovery_worker:
            del _lease_context.worker_id

    return log

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def lines_factory_for_job(job):
    """Callable that re-reads a path-backed job after validating its manifest.

    The manifest check happens before yielding any line and again after the
    final line. A changed/missing source raises ``SourceChangedError`` rather
    than silently reusing old positions for new credentials.
    """
    path = (job.source_path or '').strip()
    if not path:
        return None
    ext = (job.options or {}).get('ext', '*')
    if not (os.path.isfile(path) or os.path.isdir(path)):
        def missing_source():
            raise SourceChangedError(
                'The recorded import source is no longer available.'
            )
        return missing_source

    def factory():
        expected_signatures = {}

        def capture_signature(filepath, signature):
            expected_signatures[os.path.abspath(filepath)] = signature

        _digest, _size, _line_count, files = _validate_source_manifest(
            job, path, ext, on_file_signature=capture_signature,
        )
        return _iter_source_lines(
            job,
            path,
            ext,
            files,
            expected_signatures=expected_signatures,
        )

    return factory


def verify_import_job(job):
    """Verify ledger/counter/source integrity without exposing source values."""
    job.refresh_from_db()
    results = {
        'job_id': str(job.job_id),
        'status': job.status,
        'discrepancies': [],
        'verified': True,
    }
    actual_committed = job.records.filter(status='committed').count()
    actual_skipped = job.records.filter(status='skipped').count()
    actual_failed = job.records.filter(status='failed').count()
    actual_pending = job.records.filter(
        status__in=('pending', 'processing'),
    ).count()
    actual_attempted = actual_committed + actual_skipped + actual_failed
    actual_total = job.records.count()

    for field, expected, actual in (
        ('total_committed', job.total_committed, actual_committed),
        ('total_skipped', job.total_skipped, actual_skipped),
        ('total_failed', job.total_failed, actual_failed),
        ('total_attempted', job.total_attempted, actual_attempted),
    ):
        if expected != actual:
            results['discrepancies'].append({
                'type': 'counter_mismatch',
                'field': field,
                'expected': expected,
                'actual': actual,
            })
            results['verified'] = False

    # Path-backed manifests are re-hashed but never reported to the caller.
    source_line_count = None
    if job.source_hash and job.source_path:
        try:
            digest, _size, source_line_count, _files = _source_manifest(
                job.source_path, ext=(job.options or {}).get('ext', '*'),
            )
            if digest != job.source_hash:
                raise SourceChangedError('Manifest mismatch.')
            if job.total_discovered != source_line_count:
                results['discrepancies'].append({
                    'type': 'counter_mismatch',
                    'field': 'total_discovered',
                    'expected': job.total_discovered,
                    'actual': source_line_count,
                })
                results['verified'] = False
        except (OSError, SourceChangedError, ValueError):
            results['discrepancies'].append({
                'type': 'source_manifest_mismatch',
            })
            results['verified'] = False
    elif not job.source_hash:
        # For iterable-backed/legacy jobs, the ledger is the only discoverable
        # source count. A path manifest (when present) remains authoritative.
        source_line_count = job.total_discovered
        if job.total_discovered != actual_total:
            results['discrepancies'].append({
                'type': 'counter_mismatch',
                'field': 'total_discovered',
                'expected': job.total_discovered,
                'actual': actual_total,
            })
            results['verified'] = False
    else:
        source_line_count = job.total_discovered
        if job.source_hash and not (job.options or {}).get('source_replayable', False):
            results['discrepancies'].append({
                'type': 'source_manifest_unavailable',
            })
            results['verified'] = False

    if source_line_count is not None:
        terminal_positions = (
            job.records.filter(
                status__in=('committed', 'skipped', 'failed'),
            )
            .order_by()
            .values_list('source_position', flat=True)
            .distinct()
            .order_by('source_position')
        )
        first_position = terminal_positions.first()
        start_position = 0 if (
            not job.source_hash and first_position == 0
        ) else 1
        position_limit = start_position + source_line_count - 1
        expected_position = start_position
        first_gap = None
        unexpected_positions = 0
        for position in terminal_positions.iterator(chunk_size=1000):
            if position < start_position:
                unexpected_positions += 1
                continue
            if position > position_limit:
                unexpected_positions += 1
                continue
            if position == expected_position:
                expected_position += 1
            elif position > expected_position and first_gap is None:
                first_gap = expected_position
        if first_gap is None and expected_position <= position_limit:
            first_gap = expected_position
        terminal_position_count = terminal_positions.count()
        unaccounted = max(0, source_line_count - terminal_position_count)
        if first_gap is not None or unaccounted:
            results['discrepancies'].append({
                'type': 'source_positions_unaccounted',
                'count': max(unaccounted, 1 if first_gap is not None else 0),
                'first_missing_position': first_gap,
            })
            results['verified'] = False
        if unexpected_positions:
            results['discrepancies'].append({
                'type': 'source_positions_outside_manifest',
                'count': unexpected_positions,
            })
            results['verified'] = False

    # A committed status must still reference an Account. New durable rows
    # also carry a fingerprint snapshot to detect a mismatched relationship.
    missing_accounts = 0
    fingerprint_mismatches = 0
    for rec in job.records.filter(status='committed').iterator(chunk_size=1000):
        account_fingerprint = None
        if rec.account_id is not None:
            account_fingerprint = Account.objects.filter(
                pk=rec.account_id,
            ).values_list('fingerprint', flat=True).first()
        if account_fingerprint is None:
            if (
                rec.account_id is not None
                or rec.account_fingerprint
                or _is_sha256_digest(rec.source_hash)
            ):
                missing_accounts += 1
        elif (
            rec.account_fingerprint
            and rec.account_fingerprint != account_fingerprint
        ):
            fingerprint_mismatches += 1
    if missing_accounts:
        results['discrepancies'].append({
            'type': 'committed_account_missing',
            'count': missing_accounts,
        })
        results['verified'] = False
    if fingerprint_mismatches:
        results['discrepancies'].append({
            'type': 'committed_account_fingerprint_mismatch',
            'count': fingerprint_mismatches,
        })
        results['verified'] = False

    now = timezone.now()
    if (
        job.status == 'processing'
        and job.lease_expires_at
        and job.lease_expires_at <= now
    ):
        results['discrepancies'].append({
            'type': 'stale_lease',
            'worker_id': job.worker_id,
            'expired_at': job.lease_expires_at.isoformat(),
        })
        results['verified'] = False

    duplicate_keys = (
        job.records.values('idempotency_key')
        .annotate(cnt=dj_models.Count('id'))
        .filter(cnt__gt=1)
    )
    if duplicate_keys.exists():
        results['discrepancies'].append({
            'type': 'duplicate_idempotency_keys',
            'count': duplicate_keys.count(),
        })
        results['verified'] = False

    results['counters'] = {
        'discovered': job.total_discovered,
        'attempted': actual_attempted,
        'committed': actual_committed,
        'skipped': actual_skipped,
        'failed': actual_failed,
        'pending': actual_pending,
        'ledger_rows': actual_total,
    }
    return results

# ---------------------------------------------------------------------------
# File-based import with durability
# ---------------------------------------------------------------------------

def durable_import_file(filepath, job=None, on_progress=None,
                        cancel_check=None, batch_rows=None,
                        on_job_created=None):
    """Import one path-backed file after pinning and verifying its manifest."""
    filepath = os.path.abspath(filepath)
    expected_signatures = {}

    def capture_signature(path, signature):
        expected_signatures[os.path.abspath(path)] = signature

    if job is None:
        digest, byte_size, line_count, files = _source_manifest(filepath)
        job = create_import_job(
            source_name=os.path.basename(filepath),
            source_path=filepath,
            source_size=byte_size,
            source_hash=digest,
        )
        job.total_discovered = line_count
        job.save(update_fields=['total_discovered'])
        # Close the gap between initial manifest creation and acquiring the
        # processing lease; content is hashed again while lines are streamed.
        _digest, _size, _line_count, files = _validate_source_manifest(
            job, filepath, on_file_signature=capture_signature,
        )
    else:
        if job.source_path and os.path.abspath(job.source_path) != filepath:
            raise SourceChangedError(
                'Import source path differs from the recorded job source.'
            )
        if not job.source_path:
            job.source_path = filepath
            job.save(update_fields=['source_path'])
        _digest, _size, _line_count, files = _validate_source_manifest(
            job, filepath, on_file_signature=capture_signature,
        )

    if on_job_created:
        on_job_created(job)

    line_iterator = _iter_source_lines(
        job,
        filepath,
        '*',
        files,
        expected_signatures=expected_signatures,
    )
    session = DurableImportSession(
        job,
        batch_rows=batch_rows,
        on_progress=on_progress,
        cancel_check=cancel_check,
    )
    session.run(line_iterator)
    return job


def _uploaded_manifest(uploaded_files, on_file_scanned=None):
    """Build a metadata-only manifest for rewindable uploaded file objects."""
    manifest = hashlib.sha256()
    manifest.update(b'credvault-upload-manifest-v1\0')
    total_size = 0
    total_lines = 0
    names = []
    for uploaded_file in uploaded_files:
        name = os.path.basename(getattr(uploaded_file, 'name', '') or 'upload')
        names.append(name)
        file_hash = hashlib.sha256()
        file_size = 0
        newline_count = 0
        last_byte = b''
        try:
            uploaded_file.seek(0)
            while True:
                chunk = uploaded_file.read(1 << 16)
                if not chunk:
                    break
                file_hash.update(chunk)
                file_size += len(chunk)
                newline_count += chunk.count(b'\n')
                last_byte = chunk[-1:]
            uploaded_file.seek(0)
        except (OSError, AttributeError, ValueError) as exc:
            raise SourceChangedError(
                'An uploaded source cannot be read reliably.'
            ) from exc
        line_count = newline_count + int(
            file_size > 0 and last_byte != b'\n'
        )
        manifest.update(name.encode('utf-8', errors='replace'))
        manifest.update(b'\0')
        manifest.update(str(file_size).encode('ascii'))
        manifest.update(b':')
        manifest.update(file_hash.digest())
        total_size += file_size
        total_lines += line_count
        if on_file_scanned is not None:
            on_file_scanned(name, file_size, line_count)
    return manifest.hexdigest(), total_size, total_lines, names


def durable_import_uploads(uploaded_files, source_name=None, on_progress=None,
                            cancel_check=None, batch_rows=None,
                            expected_line_count=None, on_job_created=None,
                            prepared_manifest=None):
    """Import uploaded streams durably; bytes are not retained for recovery."""
    uploaded_files = list(uploaded_files)
    if not uploaded_files:
        raise ValueError('At least one uploaded source is required.')
    if prepared_manifest is None:
        digest, byte_size, line_count, names = _uploaded_manifest(uploaded_files)
    else:
        digest, byte_size, line_count, names = prepared_manifest
        names = list(names)
    if expected_line_count is not None and line_count != expected_line_count:
        raise SourceChangedError(
            'Uploaded source line count changed after progress was measured.'
        )
    job = create_import_job(
        source_name=source_name or f'{len(uploaded_files)} uploaded file(s)',
        source_size=byte_size,
        source_hash=digest,
        options={
            'source_kind': 'upload',
            'source_replayable': False,
            'file_names': names,
        },
    )
    job.total_discovered = line_count
    job.save(update_fields=['total_discovered'])
    if on_job_created:
        on_job_created(job)

    def all_lines():
        manifest = hashlib.sha256()
        manifest.update(b'credvault-upload-manifest-v1\0')
        actual_size = 0
        actual_lines = 0
        actual_names = []
        try:
            for uploaded_file in uploaded_files:
                name = os.path.basename(
                    getattr(uploaded_file, 'name', '') or 'upload'
                )
                actual_names.append(name)
                file_hash = hashlib.sha256()
                file_size = 0
                newline_count = 0
                last_byte = b''
                uploaded_file.seek(0)
                for line in uploaded_file:
                    line_bytes = (
                        line.encode('utf-8', errors='replace')
                        if isinstance(line, str) else bytes(line)
                    )
                    file_hash.update(line_bytes)
                    file_size += len(line_bytes)
                    newline_count += line_bytes.count(b'\n')
                    if line_bytes:
                        last_byte = line_bytes[-1:]
                    yield line
                line_count_for_file = newline_count + int(
                    file_size > 0 and last_byte != b'\n'
                )
                manifest.update(name.encode('utf-8', errors='replace'))
                manifest.update(b'\0')
                manifest.update(str(file_size).encode('ascii'))
                manifest.update(b':')
                manifest.update(file_hash.digest())
                actual_size += file_size
                actual_lines += line_count_for_file

            if (
                manifest.hexdigest() != digest
                or actual_size != byte_size
                or actual_lines != line_count
                or actual_names != names
            ):
                raise SourceChangedError(
                    'Uploaded source changed while the import was running.'
                )
        except SourceChangedError:
            raise
        except (OSError, AttributeError, TypeError, ValueError) as exc:
            raise SourceChangedError(
                'An uploaded source became unavailable while it was read.'
            ) from exc
        finally:
            for uploaded_file in uploaded_files:
                try:
                    uploaded_file.seek(0)
                except (OSError, AttributeError, TypeError, ValueError):
                    pass

    session = DurableImportSession(
        job,
        batch_rows=batch_rows,
        on_progress=on_progress,
        cancel_check=cancel_check,
    )
    session.run(all_lines())
    return job

def durable_import_path(target_path, ext='*', on_progress=None,
                        cancel_check=None, batch_rows=None,
                        expected_line_count=None, on_job_created=None,
                        prepared_manifest=None):
    """Import all matching files using one immutable path manifest/job."""
    target_path = os.path.abspath(target_path)
    if prepared_manifest is None:
        digest, byte_size, total_lines, files = _source_manifest(
            target_path, ext=ext,
        )
    else:
        digest, byte_size, total_lines, files = prepared_manifest
        files = list(files)
    if expected_line_count is not None and total_lines != expected_line_count:
        raise SourceChangedError(
            'Import source line count changed after progress was measured.'
        )
    job = create_import_job(
        source_name=os.path.basename(os.path.normpath(target_path)),
        source_path=target_path,
        source_size=byte_size,
        source_hash=digest,
        options={'ext': ext},
    )
    job.total_discovered = total_lines
    job.save(update_fields=['total_discovered'])
    if on_job_created:
        on_job_created(job)

    expected_signatures = {}

    def validate_before_stream():
        def capture_signature(filepath, signature):
            expected_signatures[os.path.abspath(filepath)] = signature

        _digest, _size, _line_count, current_files = (
            _validate_source_manifest(
                job,
                target_path,
                ext,
                on_file_signature=capture_signature,
            )
        )
        return current_files

    line_iterator = _iter_source_lines(
        job,
        target_path,
        ext,
        files,
        before_stream=validate_before_stream,
        expected_signatures=expected_signatures,
    )
    session = DurableImportSession(
        job,
        batch_rows=batch_rows,
        on_progress=on_progress,
        cancel_check=cancel_check,
    )
    session.run(line_iterator)
    return job
