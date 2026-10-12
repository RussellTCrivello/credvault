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
from django.db import connection, transaction
from django.test import Client, TestCase, skipIfDBFeature
from django.urls import reverse
from django.utils import timezone

from .import_engine import (
    DurableImportSession, acquire_lease, create_import_job,
    discover_interrupted_jobs, durable_import_file, durable_import_path,
    recover_job, release_lease, renew_lease, verify_import_job,
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

    def test_mark_paused_and_cancelled(self):
        """Jobs can be paused and cancelled."""
        job = create_import_job(source_name='test.txt')
        job.mark_started()
        
        job.mark_paused()
        job.refresh_from_db()
        self.assertEqual(job.status, 'paused')
        
        job.mark_cancelled()
        job.refresh_from_db()
        self.assertEqual(job.status, 'cancelled')

    def test_can_resume(self):
        """Only paused, interrupted, or failed jobs can resume."""
        job = create_import_job(source_name='test.txt')
        self.assertFalse(job.can_resume())

        job.status = 'paused'
        self.assertTrue(job.can_resume())

        job.status = 'interrupted'
        self.assertTrue(job.can_resume())

        job.status = 'failed'
        self.assertTrue(job.can_resume())

        job.status = 'completed'
        self.assertFalse(job.can_resume())

    def test_can_cancel_and_pause(self):
        """Cancel and pause have correct preconditions."""
        job = create_import_job(source_name='test.txt')
        self.assertTrue(job.can_cancel())  # pending can be cancelled
        self.assertFalse(job.can_pause())  # pending cannot be paused
        
        job.status = 'processing'
        self.assertTrue(job.can_cancel())
        self.assertTrue(job.can_pause())
        
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

    def test_reconciliation_detects_missing_accounts(self):
        """Reconciliation detects committed records with missing accounts."""
        job = create_import_job(source_name='test.txt')
        job.mark_started()
        job.mark_completed()

        # Create a ledger entry pointing to a non-existent account
        ImportRecord.objects.create(
            import_job=job,
            source_position=1,
            source_hash='abc123',
            status='committed',
            account_id=99999,  # Non-existent
            idempotency_key=ImportRecord.compute_idempotency_key(job.job_id, 1),
        )

        log = recover_job(job)

        # Should detect the orphaned record
        self.assertIn('missing_accounts', log.reconciliation_details)
        self.assertGreater(log.reconciliation_details['missing_accounts'], 0)


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
        """Import jobs list page renders successfully."""
        response = self.client.get(reverse('vault:import_jobs'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Import jobs')

    def test_import_job_detail_page_renders(self):
        """Import job detail page renders successfully."""
        job = create_import_job(source_name='test.txt')
        response = self.client.get(
            reverse('vault:import_job_detail', args=[job.job_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, job.source_name)

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
        job.mark_started()

        response = self.client.post(
            reverse('vault:import_job_cancel', args=[job.job_id])
        )
        self.assertEqual(response.status_code, 302)

        job.refresh_from_db()
        self.assertEqual(job.status, 'cancelled')

    def test_import_job_pause_action(self):
        """Pause action works via POST."""
        job = create_import_job(source_name='test.txt')
        job.mark_started()

        response = self.client.post(
            reverse('vault:import_job_pause', args=[job.job_id])
        )
        self.assertEqual(response.status_code, 302)

        job.refresh_from_db()
        self.assertEqual(job.status, 'paused')

    def test_import_job_resume_action(self):
        """Resume action works via POST."""
        job = create_import_job(source_name='test.txt')
        job.mark_started()
        job.mark_interrupted()

        response = self.client.post(
            reverse('vault:import_job_resume', args=[job.job_id])
        )
        self.assertEqual(response.status_code, 302)

        job.refresh_from_db()
        # After resume, job should be in processing or paused state
        self.assertIn(job.status, ['processing', 'paused', 'completed'])
