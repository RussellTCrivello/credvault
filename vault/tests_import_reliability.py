"""
Adversarial tests for the durable import engine.

These tests verify that the import system can survive interruptions,
maintain data integrity, and provide accurate recovery information.

Tests cover:
1. ImportJob lifecycle (creation, state transitions, counters)
2. ImportRecord ledger (idempotency, status tracking, account linkage)
3. Lease management (acquisition, renewal, release, expiration)
4. Durable batch commits (atomic transactions, correct outcomes)
5. Recovery after interruption (reconciliation, counter recalculation)
6. Verification (counter consistency, orphan detection, stale leases)
7. Integration with import paths (views, management command)
8. Idempotency (duplicate imports, repeated processing)
9. Concurrency (lease-based worker coordination)
10. Controlled failure injection (worker killed after a committed batch
    but before acknowledgement; resume reconciles exactly)

Note: These tests require PostgreSQL. They will be skipped if the
database is not available.
"""

import hashlib
import os
import shutil
import tempfile
import time
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase, skipIfDBFeature
from django.urls import reverse
from django.utils import timezone

from . import import_engine
from .import_engine import (
    DurableImportSession, acquire_lease, create_import_job,
    discover_interrupted_jobs, durable_import_file, durable_import_path,
    lines_factory_for_job, recover_job, release_lease, renew_lease,
    verify_import_job,
    _durable_flush_batch, _hash_source_line, _write_ledger_entries,
)
from .models import (
    Account, Email, ImportJob, ImportRecord, ImportRecoveryLog, Ip, Pass,
    Url, User,
)
from .utils import parse_line


# Check if PostgreSQL is available
def postgresql_available():
    """Check if we can connect to PostgreSQL."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return True
    except Exception:
        return False


requires_postgres = skipIfDBFeature(
    'supports_transactions',  # Inverted: skip if feature exists
    "Tests require PostgreSQL"
)


def _synthetic_account(
    url_name='https://reconcile.example/login',
    email_name='ledger@reconcile.example',
    password_name='synthetic-secret-1',
):
    """A real Account row built from synthetic values."""
    return Account.objects.create(
        url=Url.objects.create(name=url_name),
        account_type='email',
        email=Email.objects.create(name=email_name),
        password=Pass.objects.create(name=password_name),
    )


def _account_fk_constraint_name():
    """Name of the vault_importrecord.account_id -> vault_account FK."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint"
            " WHERE conrelid = 'vault_importrecord'::regclass"
            " AND confrelid = 'vault_account'::regclass"
            " AND contype = 'f'"
        )
        row = cursor.fetchone()
    return row[0] if row else None


class ImportJobModelTests(TestCase):
    """Test the ImportJob model lifecycle methods."""

    def test_create_import_job(self):
        """Jobs start in pending state with correct defaults."""
        job = create_import_job(
            source_name='test.txt',
            source_path='/tmp/test.txt',
            source_size=1234,
            source_hash='abc123',
        )
        self.assertEqual(job.status, 'pending')
        self.assertEqual(job.stage, 'initializing')
        self.assertEqual(job.source_name, 'test.txt')
        self.assertEqual(job.source_size, 1234)
        self.assertIsNotNone(job.job_id)
        self.assertEqual(job.total_committed, 0)

    def test_mark_started(self):
        """Jobs transition to processing when started."""
        job = create_import_job(source_name='test.txt')
        job.mark_started(worker_id='worker-1')
        job.refresh_from_db()
        self.assertEqual(job.status, 'processing')
        self.assertEqual(job.stage, 'importing')
        self.assertIsNotNone(job.started_at)
        self.assertEqual(job.worker_id, 'worker-1')
        self.assertIsNotNone(job.lease_expires_at)

    def test_mark_completed(self):
        """Jobs can be marked completed."""
        job = create_import_job(source_name='test.txt')
        job.mark_completed(has_errors=False)
        job.refresh_from_db()
        self.assertEqual(job.status, 'completed')
        self.assertEqual(job.stage, 'complete')
        self.assertIsNotNone(job.completed_at)

    def test_mark_completed_with_errors(self):
        """Jobs can be marked completed with errors."""
        job = create_import_job(source_name='test.txt')
        job.mark_completed(has_errors=True)
        job.refresh_from_db()
        self.assertEqual(job.status, 'completed_with_errors')

    def test_mark_failed(self):
        """Jobs can be marked failed with error message."""
        job = create_import_job(source_name='test.txt')
        job.mark_failed('Something went wrong')
        job.refresh_from_db()
        self.assertEqual(job.status, 'failed')
        self.assertIn('Something went wrong', job.last_error)
        self.assertIsNotNone(job.completed_at)

    def test_mark_interrupted(self):
        """Jobs can be marked interrupted."""
        job = create_import_job(source_name='test.txt')
        job.mark_started()
        job.mark_interrupted()
        job.refresh_from_db()
        self.assertEqual(job.status, 'interrupted')

    def test_final_pause_race_does_not_leave_a_fully_imported_job_paused(self):
        """A worker may finish if pause arrives after its last source line."""
        job = create_import_job(source_name='test.txt')
        self.assertTrue(job.mark_started(worker_id='final-boundary-worker'))
        self.assertTrue(job.mark_paused(worker_id='final-boundary-worker'))

        self.assertTrue(job.mark_completed(worker_id='final-boundary-worker'))
        job.refresh_from_db()
        self.assertEqual(job.status, 'completed')
        self.assertEqual(job.worker_id, '')
        self.assertIsNone(job.lease_expires_at)

    def test_interruption_during_pause_is_recoverable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, 'synthetic-source.txt')
            with open(source_path, 'w', encoding='utf-8') as source:
                source.write('https://synthetic.example/login|user|secret\n')
            job = create_import_job(
                source_name='synthetic-source.txt', source_path=source_path,
            )
            self.assertTrue(job.mark_started(
                worker_id='interrupted-paused-worker',
            ))
            self.assertTrue(job.mark_paused(
                worker_id='interrupted-paused-worker',
            ))

            self.assertTrue(job.mark_interrupted(
                worker_id='interrupted-paused-worker',
            ))
            release_lease(job, 'interrupted-paused-worker')
            job.refresh_from_db()
            self.assertEqual(job.status, 'interrupted')
            self.assertTrue(job.can_resume())

    def test_mark_paused_resumed_and_cancelled(self):
        """A live worker can pause, continue in place, and be cancelled."""
        job = create_import_job(source_name='test.txt')
        self.assertTrue(job.mark_started(worker_id='lifecycle-worker'))

        self.assertTrue(job.mark_paused())
        job.refresh_from_db()
        self.assertEqual(job.status, 'paused')
        self.assertTrue(job.can_resume())
        self.assertTrue(job.mark_resumed())
        job.refresh_from_db()
        self.assertEqual(job.status, 'processing')

        self.assertTrue(job.mark_paused(worker_id='lifecycle-worker'))
        self.assertTrue(job.mark_cancelled())
        job.refresh_from_db()
        self.assertEqual(job.status, 'cancelled')
        self.assertEqual(job.worker_id, 'lifecycle-worker')
        release_lease(job, 'lifecycle-worker')

    def test_can_resume_requires_a_live_worker_or_replayable_source(self):
        job = create_import_job(source_name='test.txt')
        self.assertFalse(job.can_resume())

        job.status = 'paused'
        self.assertFalse(job.can_resume())
        job.worker_id = 'live-upload-worker'
        job.lease_expires_at = timezone.now() + timezone.timedelta(minutes=5)
        self.assertTrue(job.can_resume())

        job.worker_id = ''
        job.lease_expires_at = None
        job.source_path = '/synthetic/source.txt'
        job.status = 'interrupted'
        self.assertTrue(job.can_resume())
        job.status = 'failed'
        self.assertTrue(job.can_resume())

        job.status = 'completed'
        self.assertFalse(job.can_resume())

    def test_can_cancel_and_pause(self):
        """Cancel works while pending/paused; pause requires a live worker."""
        job = create_import_job(source_name='test.txt')
        self.assertTrue(job.can_cancel())
        self.assertFalse(job.can_pause())

        job.status = 'processing'
        self.assertTrue(job.can_cancel())
        self.assertFalse(job.can_pause())
        job.worker_id = 'live-worker'
        job.lease_expires_at = timezone.now() + timezone.timedelta(minutes=5)
        self.assertTrue(job.can_pause())

        job.status = 'paused'
        self.assertTrue(job.can_cancel())
        self.assertFalse(job.can_pause())

        job.status = 'completed'
        self.assertFalse(job.can_cancel())
        self.assertFalse(job.can_pause())

    def test_refresh_counters(self):
        """Counters are recalculated from ImportRecord rows."""
        job = create_import_job(source_name='test.txt')
        
        # Create some ledger entries
        ImportRecord.objects.create(
            import_job=job,
            source_position=1,
            source_hash='hash1',
            status='committed',
            idempotency_key=ImportRecord.compute_idempotency_key(job.job_id, 1),
        )
        ImportRecord.objects.create(
            import_job=job,
            source_position=2,
            source_hash='hash2',
            status='committed',
            idempotency_key=ImportRecord.compute_idempotency_key(job.job_id, 2),
        )
        ImportRecord.objects.create(
            import_job=job,
            source_position=3,
            source_hash='hash3',
            status='skipped',
            idempotency_key=ImportRecord.compute_idempotency_key(job.job_id, 3),
        )
        
        job.refresh_counters()
        job.refresh_from_db()
        
        self.assertEqual(job.total_discovered, 3)
        self.assertEqual(job.total_committed, 2)
        self.assertEqual(job.total_skipped, 1)
        self.assertEqual(job.total_failed, 0)

    def test_progress_percent(self):
        """Progress percentage is calculated correctly."""
        job = create_import_job(source_name='test.txt')
        job.total_discovered = 100
        job.total_committed = 50
        job.total_skipped = 30
        job.total_failed = 10
        
        self.assertEqual(job.progress_percent, 90.0)


class ImportRecordModelTests(TestCase):
    """Test the ImportRecord model and idempotency keys."""

    def test_compute_idempotency_key_deterministic(self):
        """Idempotency keys are deterministic for same inputs."""
        job_id = '12345678-1234-1234-1234-123456789abc'
        key1 = ImportRecord.compute_idempotency_key(job_id, 1)
        key2 = ImportRecord.compute_idempotency_key(job_id, 1)
        self.assertEqual(key1, key2)
        self.assertEqual(len(key1), 64)  # SHA-256 truncated to 64 chars

    def test_different_positions_different_keys(self):
        """Different positions produce different keys."""
        job_id = '12345678-1234-1234-1234-123456789abc'
        key1 = ImportRecord.compute_idempotency_key(job_id, 1)
        key2 = ImportRecord.compute_idempotency_key(job_id, 2)
        self.assertNotEqual(key1, key2)

    def test_different_jobs_different_keys(self):
        """Different jobs produce different keys even for same position."""
        job_id1 = '12345678-1234-1234-1234-123456789abc'
        job_id2 = '87654321-4321-4321-4321-cba987654321'
        key1 = ImportRecord.compute_idempotency_key(job_id1, 1)
        key2 = ImportRecord.compute_idempotency_key(job_id2, 1)
        self.assertNotEqual(key1, key2)


class LeaseManagementTests(TestCase):
    """Test lease acquisition, renewal, and release."""

    def test_acquire_lease_success(self):
        """Leases can be acquired for unclaimed jobs."""
        job = create_import_job(source_name='test.txt')
        acquired = acquire_lease(job, 'worker-1')
        self.assertTrue(acquired)
        job.refresh_from_db()
        self.assertEqual(job.worker_id, 'worker-1')
        self.assertIsNotNone(job.lease_expires_at)

    def test_acquire_lease_fails_for_locked_job(self):
        """Cannot acquire lease if another worker holds it."""
        job = create_import_job(source_name='test.txt')
        acquire_lease(job, 'worker-1')
        
        # Try to acquire with different worker
        job2 = ImportJob.objects.get(pk=job.pk)
        acquired = acquire_lease(job2, 'worker-2')
        self.assertFalse(acquired)

    def test_acquire_lease_succeeds_for_expired_lease(self):
        """Can acquire lease if previous lease expired."""
        job = create_import_job(source_name='test.txt')
        acquire_lease(job, 'worker-1')
        
        # Expire the lease
        job.lease_expires_at = timezone.now() - timezone.timedelta(hours=1)
        job.save()
        
        # Now another worker can acquire
        job2 = ImportJob.objects.get(pk=job.pk)
        acquired = acquire_lease(job2, 'worker-2')
        self.assertTrue(acquired)

    def test_renew_lease(self):
        """Leases can be renewed."""
        job = create_import_job(source_name='test.txt')
        acquire_lease(job, 'worker-1')
        old_expiry = job.lease_expires_at
        
        time.sleep(0.1)
        renew_lease(job)
        job.refresh_from_db()
        
        self.assertGreater(job.lease_expires_at, old_expiry)

    def test_release_lease(self):
        """Leases can be released."""
        job = create_import_job(source_name='test.txt')
        acquire_lease(job, 'worker-1')
        release_lease(job)
        
        job.refresh_from_db()
        self.assertEqual(job.worker_id, '')
        self.assertIsNone(job.lease_expires_at)


class DurableImportSessionBasicTests(TestCase):
    """Test DurableImportSession with basic scenarios."""

    def test_basic_import_creates_ledger_entries(self):
        """Basic import creates ImportRecord entries."""
        job = create_import_job(source_name='test.txt')
        
        lines = [
            b'https://example.com/login|user1@example.com|secret1|10.0.0.1\n',
            b'https://example.com/login|user2@example.com|secret2|10.0.0.2\n',
        ]
        
        session = DurableImportSession(job)
        session.run(iter(lines))
        
        job.refresh_from_db()
        self.assertEqual(job.status, 'completed')
        self.assertEqual(job.records.count(), 2)
        self.assertEqual(job.total_discovered, 2)

    def test_invalid_lines_recorded_as_skipped(self):
        """Invalid lines are recorded as skipped."""
        job = create_import_job(source_name='test.txt')
        
        lines = [
            b'https://example.com/login|user1@example.com|secret1|10.0.0.1\n',
            b'not a valid line\n',
            b'https://example.com/login|user2@example.com|secret2|10.0.0.2\n',
        ]
        
        session = DurableImportSession(job)
        session.run(iter(lines))
        
        job.refresh_from_db()
        self.assertEqual(job.total_discovered, 3)
        self.assertEqual(job.records.filter(status='skipped').count(), 1)
        self.assertEqual(
            job.records.filter(error_category='invalid_format').count(), 1
        )


class RecoveryTests(TestCase):
    """Test recovery of interrupted import jobs."""

    def test_discover_interrupted_jobs(self):
        """Find jobs that were interrupted or have stale leases."""
        job1 = create_import_job(source_name='job1.txt')
        job1.status = 'interrupted'
        job1.save()

        job2 = create_import_job(source_name='job2.txt')
        job2.status = 'processing'
        job2.lease_expires_at = timezone.now() - timezone.timedelta(hours=1)
        job2.save()

        job3 = create_import_job(source_name='job3.txt')
        job3.status = 'completed'
        job3.save()

        interrupted = discover_interrupted_jobs()
        self.assertIn(job1, interrupted)
        self.assertIn(job2, interrupted)
        self.assertNotIn(job3, interrupted)

    def test_recover_interrupted_job(self):
        """Recover an interrupted job and recalculate counters."""
        job = create_import_job(source_name='test.txt')
        job.mark_started()
        job.mark_interrupted()
        
        # Create some ledger entries
        ImportRecord.objects.create(
            import_job=job,
            source_position=1,
            source_hash='hash1',
            status='committed',
            idempotency_key=ImportRecord.compute_idempotency_key(job.job_id, 1),
        )
        ImportRecord.objects.create(
            import_job=job,
            source_position=2,
            source_hash='hash2',
            status='skipped',
            idempotency_key=ImportRecord.compute_idempotency_key(job.job_id, 2),
        )
        
        log = recover_job(job)
        
        job.refresh_from_db()
        self.assertEqual(log.outcome, 'success')
        self.assertIsNotNone(log.recovery_completed_at)
        self.assertEqual(job.total_committed, 1)
        self.assertEqual(job.total_skipped, 1)

    def test_recovery_persists_counters_and_is_idempotent(self):
        """Regression: recovery recalculated counters from the ledger but
        never persisted them on the completion path -- mark_completed()
        saves only its own status fields -- so refresh_from_db() still saw
        the stale pre-recovery zeros.  Counters must reach the database,
        and running recovery again must change nothing."""
        job = create_import_job(source_name='test.txt')
        job.mark_started()
        job.mark_interrupted()
        for position, status in (
            (1, 'committed'), (2, 'committed'), (3, 'skipped'),
        ):
            ImportRecord.objects.create(
                import_job=job,
                source_position=position,
                source_hash=f'hash{position}',
                status=status,
                idempotency_key=ImportRecord.compute_idempotency_key(
                    job.job_id, position,
                ),
            )

        log = recover_job(job)
        job.refresh_from_db()
        self.assertEqual(log.outcome, 'success')
        self.assertEqual(job.status, 'completed')
        self.assertEqual(
            (job.total_discovered, job.total_committed,
             job.total_skipped, job.total_failed),
            (3, 2, 1, 0),
        )
        self.assertEqual(job.recovery_attempts, 1)

        # A second recovery run is idempotent: same counters, no new rows.
        log2 = recover_job(ImportJob.objects.get(pk=job.pk))
        job.refresh_from_db()
        self.assertEqual(log2.outcome, 'success')
        self.assertEqual(
            (job.total_discovered, job.total_committed,
             job.total_skipped, job.total_failed),
            (3, 2, 1, 0),
        )
        self.assertEqual(job.records.count(), 3)
        self.assertEqual(job.recovery_attempts, 2)
        self.assertEqual(
            ImportRecoveryLog.objects.filter(import_job=job).count(), 2
        )

    def test_foreign_key_rejects_orphaned_account_reference(self):
        """The production foreign key -- not reconciliation -- is the first
        line of defense: a ledger row pointing at a non-existent account
        cannot be inserted.

        Django creates PostgreSQL foreign keys as DEFERRABLE INITIALLY
        DEFERRED, so inside a TestCase transaction the check fires at
        commit (or at ``SET CONSTRAINTS ... IMMEDIATE``) rather than at
        the INSERT.  The savepoint below switches the constraint to
        immediate -- exactly how production behaves under autocommit --
        to prove the insert is rejected.
        """
        job = create_import_job(source_name='test.txt')
        idem_key = ImportRecord.compute_idempotency_key(job.job_id, 1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
                ImportRecord.objects.create(
                    import_job=job,
                    source_position=1,
                    source_hash='abc123',
                    status='committed',
                    account_id=99999,  # Non-existent
                    idempotency_key=idem_key,
                )
        self.assertFalse(
            ImportRecord.objects.filter(idempotency_key=idem_key).exists()
        )

    def test_reconciliation_detects_missing_accounts(self):
        """Reconciliation defensively detects a committed ledger row whose
        account row is gone.

        The production foreign key makes that state unreachable through
        supported paths: inserting a dangling account_id is rejected (see
        test_foreign_key_rejects_orphaned_account_reference) and deleting
        an Account NULLs the link via SET_NULL.  To exercise recover_job's
        defensive handling anyway, the impossible state is simulated here
        by dropping and re-adding the FK inside this test's own
        transaction, on the disposable test database only.  The schema
        change is rolled back at teardown; production constraints and
        migrations are untouched.
        """
        job = create_import_job(source_name='test.txt')
        job.mark_started()
        job.mark_interrupted()
        account = _synthetic_account()
        record = ImportRecord.objects.create(
            import_job=job,
            source_position=1,
            source_hash='abc123',
            status='committed',
            account=account,
            idempotency_key=ImportRecord.compute_idempotency_key(
                job.job_id, 1,
            ),
        )

        constraint = _account_fk_constraint_name()
        self.assertIsNotNone(constraint)
        with transaction.atomic(), connection.cursor() as cursor:
            # Django's FKs are DEFERRABLE INITIALLY DEFERRED, so the test's
            # earlier inserts left their checks queued; fire them now, or
            # ALTER TABLE refuses to run ("pending trigger events").
            cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
            cursor.execute(
                f'ALTER TABLE vault_importrecord DROP CONSTRAINT {constraint}'
            )
            # Raw DELETE bypasses Django's SET_NULL collector, leaving the
            # dangling reference a real foreign key would never allow.
            cursor.execute(
                'DELETE FROM vault_account WHERE id = %s', [account.pk]
            )

        # Recovery runs inside the test-only window where the FK is absent
        # (its reclassification UPDATE of the dangling row would otherwise
        # trip the freshly re-added constraint's trigger).
        log = recover_job(job)

        with transaction.atomic(), connection.cursor() as cursor:
            # NOT VALID: re-add the constraint without validating the one
            # row this test deliberately corrupted; the TestCase rollback
            # at teardown restores the original, fully valid constraint.
            cursor.execute(
                f'ALTER TABLE vault_importrecord ADD CONSTRAINT {constraint}'
                ' FOREIGN KEY (account_id) REFERENCES vault_account (id)'
                ' NOT VALID'
            )

        # The orphaned record is detected and reported.
        self.assertIn('missing_accounts', log.reconciliation_details)
        self.assertEqual(log.reconciliation_details['missing_accounts'], 1)
        self.assertEqual(
            log.reconciliation_details['orphaned_account_ids'],
            [account.pk],
        )
        self.assertEqual(log.outcome, 'partial')

        # It is also defensively handled: reclassified as failed, with the
        # recalculated counters persisted to the database.
        record.refresh_from_db()
        self.assertEqual(record.status, 'failed')
        self.assertEqual(record.error_category, 'reconciliation_missing')
        job.refresh_from_db()
        self.assertEqual(job.status, 'completed_with_errors')
        self.assertEqual(job.total_committed, 0)
        self.assertEqual(job.total_failed, 1)
        self.assertEqual(job.total_discovered, 1)


class _SimulatedWorkerKill(BaseException):
    """Stand-in for SIGKILL / TerminateProcess.

    Derives from BaseException so it slips past the session's ``except
    Exception`` failure handler exactly like a real process kill: the job
    is left 'processing' with a lease nobody will ever renew.
    """


class CrashRecoveryTests(TestCase):
    """
    Controlled failure injection around the durable engine.

    The worker is killed after a committed batch but before the caller
    receives the acknowledgement; the same job is then resumed and must
    finish without duplicating committed records, with ledger counters
    that reconcile exactly.  Only the disposable test database and
    synthetic records are used.
    """

    LINES = [
        b'https://crash.example/login|user1@crash.example|synthetic-secret-1|10.9.0.1\n',
        b'https://crash.example/login|user2@crash.example|synthetic-secret-2|10.9.0.2\n',
        b'https://crash.example/login|user3@crash.example|synthetic-secret-3|10.9.0.3\n',
        b'this line is not a valid record\n',
        b'https://crash.example/login|user5@crash.example|synthetic-secret-5|10.9.0.5\n',
        b'https://crash.example/login|user6@crash.example|synthetic-secret-6|10.9.0.6\n',
        b'https://crash.example/login|user7@crash.example|synthetic-secret-7|10.9.0.7\n',
        b'https://crash.example/login|user8@crash.example|synthetic-secret-8|10.9.0.8\n',
    ]

    def setUp(self):
        administrator = get_user_model().objects.create_user(
            username='recovery-admin',
            password='test-password',
            is_staff=True,
        )
        self.client.force_login(administrator)
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)
        self.source_path = os.path.join(self.tmp_dir, 'crash-source.txt')
        with open(self.source_path, 'wb') as handle:
            handle.writelines(self.LINES)

    def _kill_worker_after(self, job, committed_batches, batch_rows=3):
        """Run the durable import, killing the worker right after the
        given number of batch flushes -- after the database commit, but
        before the session (the caller) has processed the result."""
        real_flush = import_engine._durable_flush_batch
        state = {'flushed': 0}

        def crashing_flush(rows, record_metas, job_pk):
            committed = real_flush(rows, record_metas, job_pk)
            state['flushed'] += 1
            if state['flushed'] >= committed_batches:
                raise _SimulatedWorkerKill()
            return committed

        with mock.patch.object(
            import_engine, '_durable_flush_batch', crashing_flush
        ):
            with self.assertRaises(_SimulatedWorkerKill):
                durable_import_file(
                    self.source_path, job=job, batch_rows=batch_rows,
                )

    def test_kill_after_commit_before_ack_resumes_without_duplicates(self):
        job = create_import_job(
            source_name='crash-source.txt',
            source_path=self.source_path,
            options={'ext': '*'},
        )
        self._kill_worker_after(job, committed_batches=2)

        # The kill left the job 'processing' with two committed batches:
        # 6 accounts and their 6 ledger rows (committed together in one
        # transaction), while the skipped-ledger row for the invalid line
        # at position 4 and the tail line at position 8 were in memory
        # only and are gone.
        job.refresh_from_db()
        self.assertEqual(job.status, 'processing')
        self.assertEqual(job.records.filter(status='committed').count(), 6)
        self.assertEqual(Account.objects.count(), 6)
        fingerprints_before = set(
            Account.objects.values_list('fingerprint', flat=True)
        )

        # Time passes: the crashed worker's lease expires and discovery
        # finds the abandoned job.
        ImportJob.objects.filter(pk=job.pk).update(
            lease_expires_at=timezone.now() - timezone.timedelta(seconds=1)
        )
        self.assertTrue(
            discover_interrupted_jobs().filter(pk=job.pk).exists()
        )

        log = recover_job(
            ImportJob.objects.get(pk=job.pk),
            lines_iterable_factory=lines_factory_for_job(job),
        )

        job.refresh_from_db()
        self.assertEqual(log.outcome, 'partial')
        self.assertIsNotNone(log.recovery_completed_at)
        self.assertEqual(job.status, 'completed')

        # Every valid line is committed exactly once: the 6 pre-crash
        # accounts are untouched, the tail line was added, and nothing
        # was duplicated.
        fingerprints_after = list(
            Account.objects.values_list('fingerprint', flat=True)
        )
        self.assertEqual(len(fingerprints_after), 7)
        self.assertEqual(len(set(fingerprints_after)), 7)
        self.assertTrue(
            fingerprints_before.issubset(set(fingerprints_after))
        )

        # The ledger reconciles exactly, with positions faithful to the
        # source: the invalid line is skipped at position 4 and all 8
        # positions are present exactly once.
        self.assertEqual(
            sorted(job.records.values_list('source_position', flat=True)),
            list(range(1, 9)),
        )
        skipped = job.records.get(status='skipped')
        self.assertEqual(skipped.source_position, 4)
        self.assertEqual(skipped.error_category, 'invalid_format')
        self.assertEqual(job.total_discovered, 8)
        self.assertEqual(job.total_committed, 7)
        self.assertEqual(job.total_skipped, 1)
        self.assertEqual(job.total_failed, 0)
        for record in job.records.filter(status='committed'):
            self.assertIsNotNone(record.account_id)
            self.assertTrue(
                Account.objects.filter(pk=record.account_id).exists()
            )

        results = verify_import_job(job)
        self.assertTrue(results['verified'], results['discrepancies'])

        # A second recovery run is idempotent: no new accounts, no new
        # ledger rows, counters unchanged.
        log2 = recover_job(ImportJob.objects.get(pk=job.pk))
        job.refresh_from_db()
        self.assertEqual(log2.outcome, 'success')
        self.assertEqual(Account.objects.count(), 7)
        self.assertEqual(job.records.count(), 8)
        self.assertEqual(
            (job.total_discovered, job.total_committed,
             job.total_skipped, job.total_failed),
            (8, 7, 1, 0),
        )

    def test_resume_view_reprocesses_abandoned_source_file(self):
        """The Resume action on the job page re-reads the recorded source
        path and finishes an abandoned job without duplicating records."""
        job = create_import_job(
            source_name='crash-source.txt',
            source_path=self.source_path,
            options={'ext': '*'},
        )
        self._kill_worker_after(job, committed_batches=1)
        ImportJob.objects.filter(pk=job.pk).update(
            lease_expires_at=timezone.now() - timezone.timedelta(seconds=1)
        )

        job.refresh_from_db()
        self.assertEqual(job.status, 'processing')
        # A processing job whose lease expired was abandoned by a crashed
        # worker, so it can be taken over.
        self.assertTrue(job.can_resume())

        response = self.client.post(
            reverse('vault:import_job_resume', args=[job.job_id])
        )
        self.assertRedirects(
            response,
            reverse('vault:import_job_detail', args=[job.job_id]),
        )

        job.refresh_from_db()
        self.assertEqual(job.status, 'completed')
        self.assertEqual(job.total_committed, 7)
        self.assertEqual(job.total_skipped, 1)
        self.assertEqual(job.total_failed, 0)
        self.assertEqual(Account.objects.count(), 7)
        self.assertEqual(job.records.count(), 8)


class VerificationTests(TestCase):
    """Test import job verification."""

    def test_verify_clean_job(self):
        """Verify a clean job with no discrepancies."""
        job = create_import_job(source_name='test.txt')
        job.mark_completed()

        # Create some ledger entries
        for i in range(5):
            ImportRecord.objects.create(
                import_job=job,
                source_position=i,
                source_hash=f'hash{i}',
                status='committed',
                idempotency_key=ImportRecord.compute_idempotency_key(job.job_id, i),
            )

        job.refresh_counters()
        results = verify_import_job(job)

        self.assertTrue(results['verified'])
        self.assertEqual(len(results['discrepancies']), 0)
        self.assertEqual(results['counters']['committed'], 5)

    def test_verify_detects_counter_mismatch(self):
        """Verify detects when counters don't match ledger."""
        job = create_import_job(source_name='test.txt')
        job.total_committed = 100  # Wrong
        job.mark_completed()

        ImportRecord.objects.create(
            import_job=job,
            source_position=1,
            source_hash='hash1',
            status='committed',
            idempotency_key=ImportRecord.compute_idempotency_key(job.job_id, 1),
        )

        results = verify_import_job(job)

        self.assertFalse(results['verified'])
        self.assertGreater(len(results['discrepancies']), 0)
        mismatch = next(
            d for d in results['discrepancies']
            if d['type'] == 'counter_mismatch'
        )
        self.assertEqual(mismatch['field'], 'total_committed')

    def test_verify_detects_stale_lease(self):
        """Verify detects stale leases."""
        job = create_import_job(source_name='test.txt')
        job.status = 'processing'
        job.worker_id = 'worker-1'
        job.lease_expires_at = timezone.now() - timezone.timedelta(hours=1)
        job.save()

        results = verify_import_job(job)

        self.assertFalse(results['verified'])
        stale = next(
            d for d in results['discrepancies']
            if d['type'] == 'stale_lease'
        )
        self.assertEqual(stale['worker_id'], 'worker-1')


class IdempotencyTests(TestCase):
    """Test idempotency of import operations."""

    def test_duplicate_idempotency_keys_prevented(self):
        """Cannot create duplicate ImportRecord entries."""
        job = create_import_job(source_name='test.txt')
        idem_key = ImportRecord.compute_idempotency_key(job.job_id, 1)
        
        ImportRecord.objects.create(
            import_job=job,
            source_position=1,
            source_hash='hash1',
            status='committed',
            idempotency_key=idem_key,
        )
        
        # Try to create another with same idempotency key
        with self.assertRaises(Exception):  # IntegrityError
            ImportRecord.objects.create(
                import_job=job,
                source_position=1,
                source_hash='hash1',
                status='committed',
                idempotency_key=idem_key,
            )


class ImportManagementViewTests(TestCase):
    """Test the import management web interface."""

    def setUp(self):
        administrator = get_user_model().objects.create_user(
            username='admin',
            password='test-password',
            is_staff=True,
        )
        self.client.force_login(administrator)

    def test_import_jobs_list_page_renders(self):
        """Import jobs list page renders and links to live controls."""
        response = self.client.get(reverse('vault:import_jobs'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Import jobs')
        self.assertContains(response, 'data-import-job-list-monitor')

    def test_import_jobs_list_reaches_pages_after_the_first_hundred(self):
        for index in range(101):
            create_import_job(source_name=f'synthetic-job-{index}.txt')

        response = self.client.get(reverse('vault:import_jobs'), {'page': 2})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].number, 2)
        self.assertEqual(response.context['page_obj'].paginator.count, 101)
        self.assertEqual(len(response.context['jobs']), 1)

    def test_import_page_exposes_live_server_job_controls(self):
        response = self.client.get(reverse('vault:import'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-import-live-jobs')
        self.assertContains(response, 'Manage active import jobs')
        self.assertContains(response, 'Import jobs')

    def test_import_job_detail_page_renders(self):
        """Import job detail page renders successfully."""
        job = create_import_job(source_name='test.txt')
        response = self.client.get(
            reverse('vault:import_job_detail', args=[job.job_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, job.source_name)
        self.assertContains(response, 'data-import-job-monitor')
        self.assertContains(response, 'Stop import')

    def test_import_job_verify_page_renders(self):
        """Import job verification page renders successfully."""
        job = create_import_job(source_name='test.txt')
        response = self.client.get(
            reverse('vault:import_job_verify', args=[job.job_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Verification')

    def test_import_job_records_page_renders(self):
        """Import job records page renders successfully."""
        job = create_import_job(source_name='test.txt')
        response = self.client.get(
            reverse('vault:import_job_records', args=[job.job_id])
        )
        self.assertEqual(response.status_code, 200)

    def test_import_job_cancel_action(self):
        """Cancel action works via POST."""
        job = create_import_job(source_name='test.txt')
        job.mark_started(worker_id='cancel-view-worker')

        response = self.client.post(
            reverse('vault:import_job_cancel', args=[job.job_id])
        )
        self.assertEqual(response.status_code, 302)

        job.refresh_from_db()
        self.assertEqual(job.status, 'cancelled')

    def test_import_job_pause_action(self):
        """Pause action works via POST."""
        job = create_import_job(source_name='test.txt')
        job.mark_started(worker_id='pause-view-worker')

        response = self.client.post(
            reverse('vault:import_job_pause', args=[job.job_id])
        )
        self.assertEqual(response.status_code, 302)

        job.refresh_from_db()
        self.assertEqual(job.status, 'paused')

    def test_import_job_pause_ajax_response_returns_persisted_status(self):
        job = create_import_job(source_name='test.txt')
        job.mark_started(worker_id='pause-ajax-worker')

        response = self.client.post(
            reverse('vault:import_job_pause', args=[job.job_id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            HTTP_ACCEPT='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertEqual(response.json()['job']['status'], 'paused')
        self.assertTrue(response.json()['job']['can_resume'])

    def test_import_job_resume_action_continues_a_live_paused_worker(self):
        job = create_import_job(source_name='test-upload.txt')
        job.options = {'source_kind': 'upload', 'source_replayable': False}
        job.save(update_fields=['options'])
        job.mark_started(worker_id='resume-view-worker')
        self.assertTrue(job.mark_paused())

        response = self.client.post(
            reverse('vault:import_job_resume', args=[job.job_id])
        )
        self.assertEqual(response.status_code, 302)

        job.refresh_from_db()
        self.assertEqual(job.status, 'processing')
        self.assertEqual(job.worker_id, 'resume-view-worker')

    def test_import_job_status_endpoint_reports_durable_counters(self):
        job = create_import_job(source_name='status-test.txt')
        job.total_discovered = 12345
        job.save(update_fields=['total_discovered'])
        job.mark_started(worker_id='status-view-worker')

        response = self.client.get(
            reverse('vault:import_job_status', args=[job.job_id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['job_id'], str(job.job_id))
        self.assertEqual(response.json()['total_discovered'], 12345)
        self.assertTrue(response.json()['can_pause'])
        self.assertTrue(response.json()['can_cancel'])
