"""
Durable import engine with checkpointing, recovery, and record-level tracking.

This module provides a persistent import job lifecycle that survives
interruptions.  Every source record gets a ledger entry (ImportRecord)
whose status is the authoritative record of what happened to it.

Key guarantees:

1. Interruption safety -- if the process is killed, the next run
   discovers the interrupted ImportJob, reconciles its ledger with
   actual database state, and resumes from the earliest unaccounted
   work.

2. Idempotent retries -- each source record has a deterministic
   idempotency key.  Re-processing cannot create duplicate ledger
   entries or duplicate Account rows.

3. Exact counters -- counters on ImportJob are recalculated from the
   ImportRecord rows after every checkpoint and on recovery.

4. Atomic batch commits -- each batch of records is committed in a
   single database transaction with synchronous_commit ON, so the
   ledger and the data are always consistent.

5. Lease-based concurrency -- only one worker can process a given
   ImportJob at a time.  Leases expire after a timeout so a crashed
   worker cannot block forever.
"""

import hashlib
import logging
import os
import signal
import threading
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

#: Maximum source-line length stored in the ledger.
MAX_STORED_LINE_LENGTH = 2000

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
    """
    Try to acquire the processing lease for an ImportJob.

    Returns True if acquired, False if another worker holds it.
    An expired lease (lease_expires_at in the past) can be taken over.
    """
    worker_id = worker_id or _current_worker_id()
    now = timezone.now()
    expires = now + timezone.timedelta(seconds=IMPORT_LEASE_SECONDS)

    updated = ImportJob.objects.filter(pk=job.pk).filter(
        dj_models.Q(worker_id='')
        | dj_models.Q(worker_id__isnull=True)
        | dj_models.Q(lease_expires_at__lte=now),
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


def renew_lease(job):
    """Extend the lease for another IMPORT_LEASE_SECONDS."""
    if not job.worker_id:
        return
    now = timezone.now()
    expires = now + timezone.timedelta(seconds=IMPORT_LEASE_SECONDS)
    ImportJob.objects.filter(pk=job.pk, worker_id=job.worker_id).update(
        lease_expires_at=expires,
        last_activity_at=now,
    )
    job.lease_expires_at = expires
    job.last_activity_at = now


def release_lease(job):
    """Release the processing lease."""
    ImportJob.objects.filter(pk=job.pk).update(
        worker_id='',
        lease_expires_at=None,
    )
    job.worker_id = ''
    job.lease_expires_at = None


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


def _hash_file(filepath):
    """SHA-256 of a file on disk."""
    h = hashlib.sha256()
    try:
        with open(filepath, 'rb') as fh:
            while True:
                chunk = fh.read(1 << 16)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return ''
    return h.hexdigest()


def _truncate_line(line, max_length=MAX_STORED_LINE_LENGTH):
    """Truncate a source line for storage in the ledger."""
    if isinstance(line, (bytes, bytearray)):
        line = line.decode('utf-8', errors='replace')
    if not isinstance(line, str):
        line = str(line)
    if len(line) > max_length:
        return line[:max_length] + '...[truncated]'
    return line


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

def _durable_flush_batch(rows, record_metas, job_pk):
    """
    Commit a batch of rows and their ledger entries in ONE atomic
    transaction with synchronous_commit ON.

    ``rows`` is a list of (url, account_type, ident, secret, ip) tuples.
    ``record_metas`` is a parallel list of dicts with source_position,
    source_hash, source_line, and idempotency_key.

    Returns the number of newly committed accounts.

    Transaction steps:
      1. COPY rows into the staging temp table.
      2. INSERT reference rows (URLs, passwords, emails, users, IPs).
      3. INSERT Account rows with RETURNING fingerprint, id.
      4. Batch-lookup reference IDs to compute fingerprints for each
         source row.
      5. Create ImportRecord rows: 'committed' for rows whose
         fingerprint was in the RETURNING set, 'skipped' for the rest.

    All in one transaction so the ledger and data are always consistent:
    a crash either loses the whole batch (resume re-processes those
    lines) or keeps accounts *and* their ledger rows together.  Writing
    the ledger after the commit would leave a window where committed
    accounts have no ledger entry and recovery would mislabel the lines
    as duplicates.
    """
    if not rows:
        return 0

    now = timezone.now()

    with transaction.atomic():
        with connection.cursor() as cursor:
            # NOTE: we intentionally do NOT set synchronous_commit = off
            # here.  The durable engine needs every commit to be
            # actually persisted before we record it in the ledger.
            cursor.execute(_STAGE_DDL)
            cursor.execute('TRUNCATE vault_import_stage')
            with cursor.copy(
                'COPY vault_import_stage (url, account_type, ident, secret, ip)'
                ' FROM STDIN'
            ) as copy_ctx:
                for row in rows:
                    copy_ctx.write_row(row)
            cursor.execute('ANALYZE vault_import_stage')

            # Insert reference rows (ON CONFLICT DO NOTHING is fine)
            for _table, sql in _REFERENCE_INSERTS:
                cursor.execute(sql, (now, now))

            # Insert accounts with RETURNING to capture newly created ones
            cursor.execute(_ACCOUNT_INSERT_RETURNING, (now, now))
            newly_created = {
                fp: acc_id for fp, acc_id in cursor.fetchall()
            }

        # Still inside the transaction: determine outcomes for each row
        # (Python fingerprints + batch reference lookups on this same
        # connection) and write the ledger entries, so accounts and their
        # ImportRecord rows commit or roll back together.
        committed_count = _write_ledger_entries(
            rows, record_metas, job_pk, newly_created, now,
        )
    return committed_count


def _write_ledger_entries(rows, record_metas, job_pk, newly_created, now):
    """
    After a batch flush, determine each row's outcome and create
    ImportRecord rows.

    ``newly_created`` maps fingerprint -> account_id for rows that were
    actually inserted by this batch.

    Returns the number of committed records.
    """
    # Batch-lookup reference IDs for fingerprint computation
    unique_urls = {r[0] for r in rows}
    unique_passwords = {r[3] for r in rows}
    unique_emails = {r[2] for r in rows if r[1] == 'email' and r[2]}
    unique_users = {r[2] for r in rows if r[1] != 'email' and r[2]}
    unique_ips = {r[4] for r in rows if r[4]}

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

    # Compute fingerprints for all rows
    all_fps = []
    for row in rows:
        url_id = url_ids.get(row[0])
        pw_id = pass_ids.get(row[3])
        if row[1] == 'email':
            email_id = email_ids.get(row[2])
            user_id = None
        else:
            email_id = None
            user_id = user_ids.get(row[2])
        ip_id = ip_ids.get(row[4]) if row[4] else None

        if not url_id or not pw_id:
            all_fps.append(None)
            continue
        if row[1] == 'email' and not email_id:
            all_fps.append(None)
            continue
        if row[1] != 'email' and not user_id:
            all_fps.append(None)
            continue

        fp = record_fingerprint(url_id, row[1], email_id, user_id, pw_id, ip_id)
        all_fps.append(fp)

    # Determine outcomes
    record_objs = []
    committed = 0

    for i, (meta, fp) in enumerate(zip(record_metas, all_fps)):
        if fp is not None and fp in newly_created:
            # This row was newly committed by this batch
            record_objs.append(ImportRecord(
                import_job_id=job_pk,
                source_position=meta['source_position'],
                source_hash=meta['source_hash'],
                source_line=meta.get('source_line', ''),
                status='committed',
                account_id=newly_created[fp],
                idempotency_key=meta['idempotency_key'],
                processed_at=now,
            ))
            committed += 1
        else:
            # Not newly created -- determine if pre-existing dup or failed
            if fp is not None:
                # Check if it exists at all
                existing = Account.objects.filter(
                    fingerprint=fp
                ).values_list('id', flat=True).first()
                if existing:
                    # Pre-existing duplicate
                    record_objs.append(ImportRecord(
                        import_job_id=job_pk,
                        source_position=meta['source_position'],
                        source_hash=meta['source_hash'],
                        source_line=meta.get('source_line', ''),
                        status='skipped',
                        error_category='duplicate_fingerprint',
                        account_id=existing,
                        idempotency_key=meta['idempotency_key'],
                        processed_at=now,
                    ))
                else:
                    # Fingerprint doesn't exist -- failed to produce account
                    record_objs.append(ImportRecord(
                        import_job_id=job_pk,
                        source_position=meta['source_position'],
                        source_hash=meta['source_hash'],
                        source_line=meta.get('source_line', ''),
                        status='failed',
                        error_category='missing_reference',
                        idempotency_key=meta['idempotency_key'],
                        processed_at=now,
                    ))
            else:
                # Couldn't compute fingerprint (missing references)
                record_objs.append(ImportRecord(
                    import_job_id=job_pk,
                    source_position=meta['source_position'],
                    source_hash=meta['source_hash'],
                    source_line=meta.get('source_line', ''),
                    status='failed',
                    error_category='missing_reference',
                    idempotency_key=meta['idempotency_key'],
                    processed_at=now,
                ))

    if record_objs:
        ImportRecord.objects.bulk_create(record_objs, ignore_conflicts=True)

    return committed


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
                 on_progress=None, cancel_check=None):
        self.job = job
        self._batch_rows = batch_rows or stage_rows()
        self._workers = workers if workers is not None else worker_count()
        self._on_progress = on_progress
        self._cancel_check = cancel_check
        self._position = 0
        self._pending_skipped = []
        self._interrupted = False
        self._paused = False
        self._cancelled = False
        self._worker_id = _current_worker_id()
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

    def _should_stop(self):
        """Check all stop conditions."""
        if self._interrupted or self._cancelled:
            return True
        if self._cancel_check and self._cancel_check():
            self._cancelled = True
            return True
        # Re-read status from DB (another process may have cancelled)
        try:
            self.job.refresh_from_db(fields=['status'])
        except Exception:
            pass
        if self.job.status in ('cancelled', 'paused'):
            if self.job.status == 'paused':
                self._paused = True
            return True
        return False

    def run(self, lines_iterable):
        """Process all lines with durable tracking."""
        self._install_signal_handlers()
        try:
            return self._run_inner(lines_iterable)
        finally:
            self._restore_signal_handlers()

    def _run_inner(self, lines_iterable):
        job = self.job

        # Acquire the processing lease
        if not acquire_lease(job, self._worker_id):
            job.mark_failed('Another worker is already processing this job.')
            return job

        job.mark_started(worker_id=self._worker_id)
        job.stage = 'importing'
        job.save(update_fields=['stage'])

        lease_counter = 0
        try:
            batch_rows = []
            batch_metas = []

            for item in lines_iterable:
                if self._should_stop():
                    # Flush partial batch at safe boundary
                    if batch_rows:
                        _durable_flush_batch(batch_rows, batch_metas, job.pk)
                    break

                if isinstance(item, PositionedLine):
                    # Recovery replay: keep the original source position so
                    # the ledger and idempotency keys stay faithful.
                    self._position = item.position
                    line = item.line
                else:
                    self._position += 1
                    line = item
                line_hash = _hash_source_line(line)
                idem_key = ImportRecord.compute_idempotency_key(
                    job.job_id, self._position,
                )

                # Parse and validate
                parsed = parse_line(line)
                record_data = _canonical_record(parsed)

                if record_data is None:
                    self._pending_skipped.append(ImportRecord(
                        import_job_id=job.pk,
                        source_position=self._position,
                        source_hash=line_hash,
                        source_line=_truncate_line(line),
                        status='skipped',
                        error_category='invalid_format',
                        idempotency_key=idem_key,
                        processed_at=timezone.now(),
                    ))
                    # Flush skipped records in groups
                    if len(self._pending_skipped) >= self._batch_rows:
                        self._flush_pending_skipped()
                else:
                    row = _row_from_parsed(record_data)
                    batch_rows.append(row)
                    batch_metas.append({
                        'source_position': self._position,
                        'source_hash': line_hash,
                        'source_line': _truncate_line(line),
                        'idempotency_key': idem_key,
                    })

                # Flush when batch is full
                if len(batch_rows) >= self._batch_rows:
                    _durable_flush_batch(batch_rows, batch_metas, job.pk)
                    batch_rows = []
                    batch_metas = []

                # Periodic lease renewal and progress
                lease_counter += 1
                if lease_counter >= 1000:
                    lease_counter = 0
                    renew_lease(job)
                    if self._on_progress:
                        self._on_progress(self._position)

            # Flush remaining batch (only if not interrupted)
            if batch_rows and not self._interrupted:
                _durable_flush_batch(batch_rows, batch_metas, job.pk)

            # Flush remaining skipped records
            if self._pending_skipped:
                self._flush_pending_skipped()

            # Set final state
            if self._cancelled:
                job.mark_cancelled()
            elif self._paused:
                job.mark_paused()
            elif self._interrupted:
                # Flush pending skipped even on interrupt
                if self._pending_skipped:
                    self._flush_pending_skipped()
                job.mark_interrupted()
            else:
                job.refresh_counters()
                has_errors = job.total_failed > 0
                job.mark_completed(has_errors=has_errors)

            release_lease(job)

        except Exception as exc:
            logger.exception("Import job %s failed: %s", job.job_id, exc)
            try:
                job.mark_failed(str(exc)[:4000])
                release_lease(job)
            except Exception:
                pass

        return job

    def _flush_pending_skipped(self):
        """Write accumulated skipped records to the database atomically."""
        if not self._pending_skipped:
            return
        with transaction.atomic():
            ImportRecord.objects.bulk_create(
                self._pending_skipped, ignore_conflicts=True,
            )
        self._pending_skipped.clear()


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def discover_interrupted_jobs():
    """Find all ImportJobs that were interrupted or have stale leases."""
    now = timezone.now()
    return ImportJob.objects.filter(
        dj_models.Q(status='interrupted')
        | dj_models.Q(
            status='processing',
            lease_expires_at__lte=now,
        )
    )


def recover_job(job, lines_iterable_factory=None):
    """
    Recover an interrupted import job.

    1. Reconcile the ImportRecord ledger with actual database state.
    2. Identify committed, pending, and failed records.
    3. Recalculate counters from authoritative ledger.
    4. Optionally resume processing if lines_iterable_factory is given.

    Returns the ImportRecoveryLog entry.
    """
    job.recovery_attempts += 1
    attempt = job.recovery_attempts

    log = ImportRecoveryLog.objects.create(
        import_job=job,
        attempt_number=attempt,
        recovery_started_at=timezone.now(),
    )

    # Mark as recovering
    job.status = 'processing'
    job.stage = 'recovering'
    job.save(update_fields=['status', 'stage', 'recovery_attempts'])

    try:
        # Step 1: Reconcile committed records with actual database state.
        # The foreign key makes a dangling account_id unreachable through
        # the ORM (inserting one is rejected and deleting an Account NULLs
        # the link via SET_NULL), but reconciliation still defends against
        # it: manual database surgery or a partial restore could leave a
        # committed ledger row pointing at a row that no longer exists.
        committed_recs = job.records.filter(status='committed')
        previously_committed = committed_recs.count()
        missing_count = 0
        orphaned_account_ids = []

        for rec in committed_recs.iterator(chunk_size=1000):
            if rec.account_id and not Account.objects.filter(
                pk=rec.account_id
            ).exists():
                rec.status = 'failed'
                rec.error_message = 'Account no longer exists in database'
                rec.error_category = 'reconciliation_missing'
                rec.save(update_fields=[
                    'status', 'error_message', 'error_category',
                ])
                missing_count += 1
                orphaned_account_ids.append(rec.account_id)

        # Step 2: Count records by status
        pending_count = job.records.filter(status='pending').count()
        failed_count = job.records.filter(status='failed').count()
        actual_committed = job.records.filter(status='committed').count()
        skipped_count = job.records.filter(status='skipped').count()
        total_records = job.records.count()

        # Step 3: Log reconciliation details
        log.records_committed = actual_committed
        log.records_pending = pending_count
        log.records_failed = failed_count
        log.reconciliation_details = {
            'missing_accounts': missing_count,
            'previously_committed': previously_committed,
            'orphaned_account_ids': orphaned_account_ids[:100],
            'total_ledger_records': total_records,
        }

        # Step 4: Recalculate counters from the authoritative ledger AND
        # persist them right away.  The mark_* transitions below save only
        # their own fields (update_fields), so without this save the
        # recalculated counters would never reach the database and a
        # refresh_from_db() would still show the stale pre-recovery values.
        job.total_discovered = total_records
        job.total_committed = actual_committed
        job.total_skipped = skipped_count
        job.total_failed = failed_count
        job.last_activity_at = timezone.now()
        job.save(update_fields=[
            'total_discovered', 'total_committed', 'total_skipped',
            'total_failed', 'last_activity_at',
        ])

        # Step 5: Resume the source when the caller can re-read it.  The
        # engine only writes ledger rows as it processes, so after a crash
        # the unaccounted work is "positions the ledger has no row for" --
        # not just rows left in 'pending'.  Positions are carried through
        # (PositionedLine) so replayed lines keep their original source
        # positions and idempotency keys.
        remaining = None
        if lines_iterable_factory is not None:
            processed_positions = set(
                job.records.exclude(status='pending')
                .values_list('source_position', flat=True)
            )
            remaining = [
                PositionedLine(position, line)
                for position, line in enumerate(
                    lines_iterable_factory(), start=1,
                )
                if position not in processed_positions
            ]

        if remaining:
            # Can actually resume processing
            log.outcome = 'partial'
            log.notes = (
                f'Resumed with {len(remaining)} unaccounted source line(s); '
                f'{previously_committed} previously committed, '
                f'{missing_count} missing accounts detected'
            )
            job.status = 'processing'
            job.stage = 'importing'
            log.save()
            job.save(update_fields=[
                'status', 'stage', 'last_activity_at',
            ])

            try:
                session = DurableImportSession(job)
                # Defensive default for any plain (unpositioned) line.
                session._position = max(
                    job.records.values_list(
                        'source_position', flat=True,
                    ),
                    default=0,
                )
                session.run(iter(remaining))
            except Exception as resume_exc:
                logger.exception(
                    "Resume of job %s failed: %s", job.job_id, resume_exc
                )
                job.mark_failed(f'Resume failed: {resume_exc}')
            log.recovery_completed_at = timezone.now()
            log.save(update_fields=['recovery_completed_at'])
        elif pending_count > 0:
            # Has pending records but no way to reprocess
            log.outcome = 'partial'
            log.notes = (
                f'Paused with {pending_count} pending records '
                f'(no lines factory provided for resume)'
            )
            job.status = 'paused'
            job.last_activity_at = timezone.now()
            log.recovery_completed_at = timezone.now()
            log.save()
            job.save(update_fields=['status', 'last_activity_at'])
        elif missing_count > 0:
            log.outcome = 'partial'
            log.notes = (
                f'Reconciliation complete: {missing_count} orphaned records'
            )
            job.mark_completed(has_errors=True)
            log.recovery_completed_at = timezone.now()
            log.save()
        else:
            log.outcome = 'success'
            log.notes = 'Reconciliation complete — all records accounted for'
            job.mark_completed(has_errors=False)
            log.recovery_completed_at = timezone.now()
            log.save()

    except Exception as exc:
        logger.exception("Recovery of job %s failed: %s", job.job_id, exc)
        log.outcome = 'failed'
        log.notes = str(exc)[:2000]
        log.recovery_completed_at = timezone.now()
        log.save()
        try:
            job.mark_failed(f'Recovery failed: {exc}')
        except Exception:
            pass

    return log


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def lines_factory_for_job(job):
    """
    Callable that re-reads a job's source lines, or None when impossible.

    Path-based imports store their source path (and the extension filter
    used to collect files) on the job, so recovery can resume them: the
    factory yields the lines in exactly the order the import session saw
    them, which is what keeps source positions and idempotency keys
    stable across a resume.  Uploaded files are not kept on disk, so
    their jobs cannot be re-read and recovery settles them instead.
    """
    path = (job.source_path or '').strip()
    if not path:
        return None
    ext = (job.options or {}).get('ext', '*')

    if os.path.isfile(path):
        def factory():
            with open(path, 'rb') as handle:
                return handle.readlines()
        return factory

    if os.path.isdir(path):
        files = collect_files_from_path(path, ext=ext)
        if not files:
            return None

        def factory():
            def lines():
                for filepath in files:
                    try:
                        with open(filepath, 'rb') as handle:
                            yield from handle
                    except OSError:
                        continue
            return lines()
        return factory

    return None


def verify_import_job(job):
    """
    Verify the integrity of an import job.

    Checks counter consistency, orphaned records, and stale leases.
    Returns a dict with results and discrepancies.
    """
    results = {
        'job_id': str(job.job_id),
        'status': job.status,
        'discrepancies': [],
        'verified': True,
    }

    actual_committed = job.records.filter(status='committed').count()
    actual_skipped = job.records.filter(status='skipped').count()
    actual_failed = job.records.filter(status='failed').count()
    actual_pending = job.records.filter(status='pending').count()
    actual_total = job.records.count()

    for field, expected, actual in [
        ('total_committed', job.total_committed, actual_committed),
        ('total_skipped', job.total_skipped, actual_skipped),
        ('total_failed', job.total_failed, actual_failed),
        ('total_discovered', job.total_discovered, actual_total),
    ]:
        if expected != actual:
            results['discrepancies'].append({
                'type': 'counter_mismatch',
                'field': field,
                'expected': expected,
                'actual': actual,
            })
            results['verified'] = False

    # Check for orphaned committed records
    orphaned = 0
    for rec in job.records.filter(
        status='committed'
    ).iterator(chunk_size=1000):
        if rec.account_id and not Account.objects.filter(
            pk=rec.account_id
        ).exists():
            orphaned += 1
    if orphaned:
        results['discrepancies'].append({
            'type': 'orphaned_committed_records',
            'count': orphaned,
        })
        results['verified'] = False

    # Check for stale lease
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

    # Check for duplicate idempotency keys (should be impossible with unique)
    dup_keys = (
        job.records.values('idempotency_key')
        .annotate(cnt=dj_models.Count('id'))
        .filter(cnt__gt=1)
    )
    if dup_keys.exists():
        results['discrepancies'].append({
            'type': 'duplicate_idempotency_keys',
            'count': dup_keys.count(),
        })
        results['verified'] = False

    results['counters'] = {
        'discovered': actual_total,
        'committed': actual_committed,
        'skipped': actual_skipped,
        'failed': actual_failed,
        'pending': actual_pending,
    }
    return results


# ---------------------------------------------------------------------------
# File-based import with durability
# ---------------------------------------------------------------------------

def durable_import_file(filepath, job=None, on_progress=None,
                        cancel_check=None, batch_rows=None):
    """
    Import a single file with durable tracking.

    If ``job`` is None, a new ImportJob is created.  Returns the job.
    """
    if job is None:
        source_hash = _hash_file(filepath)
        try:
            source_size = os.path.getsize(filepath)
        except OSError:
            source_size = None
        source_name = os.path.basename(filepath)
        job = create_import_job(
            source_name=source_name,
            source_path=filepath,
            source_size=source_size,
            source_hash=source_hash,
        )

    def line_generator():
        with open(filepath, 'rb') as fh:
            for line in fh:
                yield line

    session = DurableImportSession(
        job,
        batch_rows=batch_rows,
        on_progress=on_progress,
        cancel_check=cancel_check,
    )
    session.run(line_generator())
    return job


def durable_import_path(target_path, ext='*', on_progress=None,
                        cancel_check=None, batch_rows=None):
    """
    Import all matching files from a path with durable tracking.

    Creates one ImportJob for the whole path.  Returns the job.
    """
    files = collect_files_from_path(target_path, ext=ext)
    if not files:
        job = create_import_job(
            source_name=os.path.basename(target_path),
            source_path=target_path,
            options={'ext': ext},
        )
        job.mark_completed()
        return job

    job = create_import_job(
        source_name=os.path.basename(target_path),
        source_path=target_path,
        options={'ext': ext},
    )

    # Count total lines for progress
    total_lines = 0
    for fp in files:
        try:
            total_lines += sum(1 for _ in open(fp, 'rb'))
        except OSError:
            continue
    job.total_discovered = total_lines
    job.save(update_fields=['total_discovered'])

    session = DurableImportSession(
        job,
        batch_rows=batch_rows,
        on_progress=on_progress,
        cancel_check=cancel_check,
    )

    def all_lines():
        for fp in files:
            try:
                with open(fp, 'rb') as fh:
                    for line in fh:
                        yield line
            except OSError:
                continue

    session.run(all_lines())
    return job
