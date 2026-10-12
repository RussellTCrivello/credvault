"""
Durable import engine with checkpointing, recovery, and record-level tracking.

This module wraps the existing high-throughput BulkImportSession with a
persistent job lifecycle that survives interruptions.  Every source record
gets a ledger entry (ImportRecord) whose status is the authoritative record
of what happened to it.
"""

import hashlib
import logging
import os
import signal
import threading
import uuid
from contextlib import contextmanager

from django.db import connection, models as dj_models, transaction
from django.utils import timezone

from .importer import (
    BulkImportSession, _flush_batch, _row_from, canonical_record,
    collect_files_from_path, stage_rows, worker_count,
)
from .models import (
    Account, ImportJob, ImportRecord, ImportRecoveryLog,
    IMPORT_LEASE_SECONDS,
)
from .utils import parse_line

logger = logging.getLogger(__name__)

MAX_STORED_LINE_LENGTH = 2000


def _current_worker_id():
    """Stable identifier for the current process."""
    return f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


def acquire_lease(job, worker_id=None):
    """Try to acquire the processing lease for an ImportJob."""
    worker_id = worker_id or _current_worker_id()
    now = timezone.now()
    expires = now + timezone.timedelta(seconds=IMPORT_LEASE_SECONDS)
    updated = ImportJob.objects.filter(pk=job.pk).filter(
        dj_models.Q(worker_id='') | dj_models.Q(worker_id__isnull=True)
        | dj_models.Q(lease_expires_at__lte=now),
    ).update(
        worker_id=worker_id, lease_expires_at=expires,
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
        lease_expires_at=expires, last_activity_at=now,
    )
    job.lease_expires_at = expires
    job.last_activity_at = now


def release_lease(job):
    """Release the processing lease."""
    ImportJob.objects.filter(pk=job.pk).update(
        worker_id='', lease_expires_at=None,
    )
    job.worker_id = ''
    job.lease_expires_at = None


def create_import_job(
    source_name, source_path='', source_size=None,
    source_hash='', import_format='auto', options=None,
):
    """Create a new ImportJob in the pending state."""
    return ImportJob.objects.create(
        source_name=source_name, source_path=source_path,
        source_size=source_size, source_hash=source_hash,
        import_format=import_format, options=options or {},
        status='pending', stage='initializing',
    )


def _hash_source_line(line):
    """SHA-256 of a source line."""
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


class DurableImportSession:
    """
    Import session that tracks every source record in the database.

    Unlike BulkImportSession (in-memory only), this writes ImportRecord
    rows and commits them alongside Account rows in atomic batches.
    """

    def __init__(self, job, batch_rows=None, workers=None,
                 on_progress=None, cancel_check=None):
        self.job = job
        self._batch_rows = batch_rows or stage_rows()
        self._workers = workers if workers is not None else worker_count()
        self._on_progress = on_progress
        self._cancel_check = cancel_check
        self._position = 0
        self._pending_records = []
        self._interrupted = False
        self._paused = False
        self._cancelled = False
        self._worker_id = _current_worker_id()
        self._original_sigterm = None
        self._original_sigint = None

    def _install_signal_handlers(self):
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
        if self._interrupted or self._cancelled:
            return True
        if self._cancel_check and self._cancel_check():
            self._cancelled = True
            return True
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
        """Process all lines from the iterable with durable tracking."""
        self._install_signal_handlers()
        try:
            return self._run_inner(lines_iterable)
        finally:
            self._restore_signal_handlers()

    def _run_inner(self, lines_iterable):
        job = self.job
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

            for line in lines_iterable:
                if self._should_stop():
                    if batch_rows:
                        self._flush_batch(batch_rows, batch_metas)
                    break

                self._position += 1
                line_hash = _hash_source_line(line)
                idem_key = ImportRecord.compute_idempotency_key(
                    job.job_id, self._position,
                )
                parsed = parse_line(line)
                record_data = canonical_record(parsed)

                if record_data is None:
                    self._pending_records.append({
                        'import_job_id': job.pk,
                        'source_position': self._position,
                        'source_hash': line_hash,
                        'source_line': _truncate_line(line),
                        'status': 'skipped',
                        'error_category': 'invalid_format',
                        'idempotency_key': idem_key,
                        'processed_at': timezone.now(),
                    })
                    # Flush skipped records in groups to avoid huge lists
                    if len(self._pending_records) >= self._batch_rows:
                        self._flush_pending_records()
                else:
                    row = _row_from(record_data)
                    batch_rows.append(row)
                    batch_metas.append({
                        'source_position': self._position,
                        'source_hash': line_hash,
                        'source_line': _truncate_line(line),
                        'idempotency_key': idem_key,
                    })

                if len(batch_rows) >= self._batch_rows:
                    self._flush_batch(batch_rows, batch_metas)
                    batch_rows = []
                    batch_metas = []

                lease_counter += 1
                if lease_counter >= 1000:
                    lease_counter = 0
                    renew_lease(job)
                    if self._on_progress:
                        self._on_progress(self._position)

            if batch_rows and not self._interrupted:
                self._flush_batch(batch_rows, batch_metas)

            if self._pending_records:
                self._flush_pending_records()

            if self._cancelled:
                job.mark_cancelled()
            elif self._paused:
                job.mark_paused()
            elif self._interrupted:
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

    def _flush_batch(self, rows, record_metas):
        """Commit a batch of rows and their ledger entries atomically."""
        if not rows:
            return
        job = self.job
        now = timezone.now()

        if self._pending_records:
            self._flush_pending_records()

        with transaction.atomic():
            created = _flush_batch(rows)

            idem_keys = [m['idempotency_key'] for m in record_metas]
            # Create ledger entries
            record_objs = []
            for meta in record_metas:
                record_objs.append(ImportRecord(
                    import_job_id=job.pk,
                    source_position=meta['source_position'],
                    source_hash=meta['source_hash'],
                    source_line=meta.get('source_line', ''),
                    status='committed',
                    idempotency_key=meta['idempotency_key'],
                    processed_at=now,
                ))

            ImportRecord.objects.bulk_create(
                record_objs, ignore_conflicts=True,
            )

            # Records that were deduplicated by the DB (ON CONFLICT DO NOTHING)
            # need their status adjusted.  The _flush_batch returns the
            # number of new Account rows.  Records beyond that were
            # duplicates and should be 'skipped'.
            total_in_batch = len(rows)
            if created < total_in_batch:
                # The last (total_in_batch - created) records were duplicates
                dup_count = total_in_batch - created
                dup_keys = idem_keys[-dup_count:]
                ImportRecord.objects.filter(
                    idempotency_key__in=dup_keys,
                ).update(status='skipped', error_category='duplicate_fingerprint')

    def _flush_pending_records(self):
        """Write accumulated skipped/failed records to the database."""
        if not self._pending_records:
            return
        objs = [ImportRecord(**data) for data in self._pending_records]
        ImportRecord.objects.bulk_create(objs, ignore_conflicts=True)
        self._pending_records.clear()


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def discover_interrupted_jobs():
    """Find all ImportJobs that were interrupted or are stale."""
    now = timezone.now()
    return ImportJob.objects.filter(
        dj_models.Q(status='interrupted')
        | dj_models.Q(status='processing', lease_expires_at__lte=now)
    )


def recover_job(job, lines_iterable_factory=None):
    """
    Recover an interrupted import job.

    Reconciles the ImportRecord ledger with actual database state,
    then optionally resumes processing.
    """
    job.recovery_attempts += 1
    attempt = job.recovery_attempts

    log = ImportRecoveryLog.objects.create(
        import_job=job,
        attempt_number=attempt,
        recovery_started_at=timezone.now(),
    )

    job.status = 'processing'
    job.stage = 'recovering'
    job.save(update_fields=['status', 'stage', 'recovery_attempts'])

    try:
        committed_recs = job.records.filter(status='committed')
        missing_count = 0
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

        pending_count = job.records.filter(status='pending').count()
        failed_count = job.records.filter(status='failed').count()

        log.records_committed = committed_recs.count() - missing_count
        log.records_pending = pending_count
        log.records_failed = failed_count + missing_count
        log.reconciliation_details = {
            'missing_accounts': missing_count,
            'previously_committed': committed_recs.count(),
        }

        job.refresh_counters()

        if pending_count > 0:
            log.outcome = 'partial'
            log.notes = f'Paused with {pending_count} pending records'
            job.status = 'paused'
        elif job.total_failed > 0:
            log.outcome = 'partial' if missing_count else 'success'
            log.notes = 'Reconciliation complete with errors'
            job.mark_completed(has_errors=True)
        else:
            log.outcome = 'success'
            log.notes = 'Reconciliation complete'
            job.mark_completed(has_errors=False)

        log.recovery_completed_at = timezone.now()
        log.save()
        job.save(update_fields=['status'])

    except Exception as exc:
        logger.exception("Recovery of job %s failed: %s", job.job_id, exc)
        log.outcome = 'failed'
        log.notes = str(exc)[:2000]
        log.recovery_completed_at = timezone.now()
        log.save()
        job.mark_failed(f'Recovery failed: {exc}')

    return log


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_import_job(job):
    """
    Verify the integrity of an import job.

    Returns a dict with verification results and any discrepancies.
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

    if job.total_committed != actual_committed:
        results['discrepancies'].append({
            'type': 'counter_mismatch', 'field': 'total_committed',
            'expected': job.total_committed, 'actual': actual_committed,
        })
        results['verified'] = False

    if job.total_skipped != actual_skipped:
        results['discrepancies'].append({
            'type': 'counter_mismatch', 'field': 'total_skipped',
            'expected': job.total_skipped, 'actual': actual_skipped,
        })
        results['verified'] = False

    if job.total_failed != actual_failed:
        results['discrepancies'].append({
            'type': 'counter_mismatch', 'field': 'total_failed',
            'expected': job.total_failed, 'actual': actual_failed,
        })
        results['verified'] = False

    if job.total_discovered != actual_total:
        results['discrepancies'].append({
            'type': 'counter_mismatch', 'field': 'total_discovered',
            'expected': job.total_discovered, 'actual': actual_total,
        })
        results['verified'] = False

    orphaned = 0
    for rec in job.records.filter(status='committed').iterator(chunk_size=1000):
        if rec.account_id and not Account.objects.filter(pk=rec.account_id).exists():
            orphaned += 1
    if orphaned:
        results['discrepancies'].append({
            'type': 'orphaned_committed_records', 'count': orphaned,
        })
        results['verified'] = False

    now = timezone.now()
    if (
        job.status == 'processing'
        and job.lease_expires_at
        and job.lease_expires_at <= now
    ):
        results['discrepancies'].append({
            'type': 'stale_lease', 'worker_id': job.worker_id,
            'expired_at': job.lease_expires_at.isoformat(),
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

def durable_import_file(filepath, job=None, on_progress=None, cancel_check=None):
    """Import a single file with durable tracking."""
    if job is None:
        source_hash = _hash_file(filepath)
        try:
            source_size = os.path.getsize(filepath)
        except OSError:
            source_size = None
        source_name = os.path.basename(filepath)
        job = create_import_job(
            source_name=source_name, source_path=filepath,
            source_size=source_size, source_hash=source_hash,
        )

    def line_generator():
        with open(filepath, 'rb') as fh:
            for line in fh:
                yield line

    session = DurableImportSession(
        job, on_progress=on_progress, cancel_check=cancel_check,
    )
    session.run(line_generator())
    return job


def durable_import_path(target_path, ext='*', on_progress=None, cancel_check=None):
    """Import all matching files from a path with durable tracking."""
    files = collect_files_from_path(target_path, ext=ext)
    if not files:
        job = create_import_job(
            source_name=os.path.basename(target_path),
            source_path=target_path,
        )
        job.mark_completed()
        return job

    job = create_import_job(
        source_name=os.path.basename(target_path),
        source_path=target_path,
    )

    total_lines = 0
    for fp in files:
        try:
            total_lines += sum(1 for _ in open(fp, 'rb'))
        except OSError:
            continue
    job.total_discovered = total_lines
    job.save(update_fields=['total_discovered'])

    session = DurableImportSession(
        job, on_progress=on_progress, cancel_check=cancel_check,
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
