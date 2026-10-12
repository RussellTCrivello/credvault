"""Independent regressions for the broader reliability/table audit.

All records below are synthetic and run only against Django's disposable
PostgreSQL test database. The subprocess test deliberately dies with
``os._exit`` after a real transaction commit to exercise the acknowledgement
loss window that TestCase savepoints cannot model.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, RequestFactory, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from .import_engine import (
    DurableImportSession,
    acquire_lease,
    create_import_job,
    discover_interrupted_jobs,
    durable_import_file,
    durable_import_path,
    lines_factory_for_job,
    recover_job,
    release_lease,
    verify_import_job,
)
from .models import Account, Email, ImportJob, ImportRecord, Pass, Url, User
from .views import _paginate_accounts, filter_accounts


class DurableOutcomeAuditTests(TestCase):
    def test_same_batch_duplicate_source_line_is_counted_once_as_committed(self):
        job = create_import_job(source_name='synthetic-duplicates.txt')
        line = (
            b'https://duplicate-audit.example/login|'
            b'audit-user@duplicate-audit.example|synthetic-only-secret|'
            b'192.0.2.41\n'
        )

        DurableImportSession(job, batch_rows=10, workers=1).run([line, line])

        job.refresh_from_db()
        self.assertEqual(Account.objects.count(), 1)
        self.assertEqual(job.records.filter(status='committed').count(), 1)
        self.assertEqual(job.records.filter(status='skipped').count(), 1)
        self.assertEqual(job.total_committed, 1)
        self.assertEqual(job.total_skipped, 1)

    def test_a_failed_ledger_row_is_not_reported_as_successful_recovery(self):
        job = create_import_job(source_name='synthetic-failure.txt')
        job.status = 'interrupted'
        job.save(update_fields=['status'])
        ImportRecord.objects.create(
            import_job=job,
            source_position=1,
            source_hash='a' * 64,
            status='failed',
            error_category='synthetic_transient',
            idempotency_key=ImportRecord.compute_idempotency_key(job.job_id, 1),
        )

        log = recover_job(job)
        job.refresh_from_db()

        self.assertNotEqual(log.outcome, 'success')
        self.assertNotEqual(job.status, 'completed')
        self.assertEqual(job.total_failed, 1)

    def test_verification_flags_committed_rows_without_an_account(self):
        job = create_import_job(source_name='synthetic-orphan.txt')
        ImportRecord.objects.create(
            import_job=job,
            source_position=1,
            source_hash='b' * 64,
            status='committed',
            account=None,
            idempotency_key=ImportRecord.compute_idempotency_key(job.job_id, 1),
        )
        job.refresh_counters()

        result = verify_import_job(job)

        self.assertFalse(result['verified'])
        self.assertTrue(any(
            item['type'] == 'committed_account_missing'
            for item in result['discrepancies']
        ))

    def test_cancelled_job_cannot_acquire_a_processing_lease(self):
        job = create_import_job(source_name='synthetic-cancelled.txt')
        job.mark_cancelled()

        self.assertFalse(acquire_lease(job, 'audit-worker'))
        job.refresh_from_db()
        self.assertEqual(job.status, 'cancelled')
        self.assertEqual(job.worker_id, '')

    def test_stale_cancel_does_not_override_a_completed_job(self):
        job = create_import_job(source_name='synthetic-cancel-race.txt')
        self.assertTrue(job.mark_started(worker_id='cancel-race-worker'))
        stale_cancel_request = ImportJob.objects.get(pk=job.pk)

        self.assertTrue(job.mark_completed(worker_id='cancel-race-worker'))
        self.assertFalse(stale_cancel_request.mark_cancelled())

        job.refresh_from_db()
        self.assertEqual(job.status, 'completed')
        self.assertEqual(stale_cancel_request.status, 'completed')

    def test_stale_worker_cannot_complete_a_cancelled_job(self):
        job = create_import_job(source_name='synthetic-complete-race.txt')
        self.assertTrue(job.mark_started(worker_id='complete-race-worker'))
        stale_worker = ImportJob.objects.get(pk=job.pk)

        self.assertTrue(job.mark_cancelled())
        self.assertFalse(stale_worker.mark_completed(
            worker_id='complete-race-worker',
        ))

        job.refresh_from_db()
        self.assertEqual(job.status, 'cancelled')
        self.assertEqual(stale_worker.status, 'cancelled')

    def test_cancel_between_claim_and_start_does_not_resurrect_job(self):
        job = create_import_job(source_name='synthetic-start-race.txt')
        self.assertTrue(acquire_lease(job, 'start-race-worker'))
        self.assertTrue(job.mark_cancelled())

        self.assertFalse(job.mark_started(worker_id='start-race-worker'))
        job.refresh_from_db()
        self.assertEqual(job.status, 'cancelled')
        self.assertEqual(job.worker_id, '')

    def test_stale_worker_cannot_release_a_replacement_workers_lease(self):
        job = create_import_job(source_name='synthetic-lease.txt')
        self.assertTrue(acquire_lease(job, 'old-worker'))
        replacement_expiry = timezone.now() + timezone.timedelta(minutes=5)
        ImportJob.objects.filter(pk=job.pk).update(
            worker_id='replacement-worker',
            lease_expires_at=replacement_expiry,
        )

        release_lease(job)

        job.refresh_from_db()
        self.assertEqual(job.worker_id, 'replacement-worker')
        self.assertEqual(job.lease_expires_at, replacement_expiry)

    def test_unstarted_path_job_can_be_discovered_and_recovered(self):
        line = (
            b'https://pending-recovery.example/login|pending-user|'
            b'synthetic-pending-secret|192.0.2.51\n'
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = Path(tmpdir) / 'pending-source.txt'
            source_path.write_bytes(line)
            job = create_import_job(
                source_name='pending-source.txt',
                source_path=str(source_path),
            )

            self.assertTrue(job.can_resume())
            self.assertTrue(
                discover_interrupted_jobs().filter(pk=job.pk).exists()
            )
            log = recover_job(
                job, lines_iterable_factory=lines_factory_for_job(job),
            )

            job.refresh_from_db()
            self.assertEqual(log.outcome, 'partial')
            self.assertEqual(job.status, 'completed')
            self.assertEqual(job.total_discovered, 1)
            self.assertEqual(job.total_committed, 1)
            self.assertEqual(Account.objects.count(), 1)

    def test_resume_refuses_a_source_file_that_changed_after_import(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = Path(tmpdir) / 'source.txt'
            source_path.write_bytes(
                b'https://source-audit.example/login|audit-user|'
                b'synthetic-secret-before|192.0.2.42\n'
            )
            job = durable_import_file(str(source_path), batch_rows=10)
            source_path.write_bytes(
                b'https://source-audit.example/login|audit-user|'
                b'synthetic-secret-after|192.0.2.42\n'
            )

            source_factory = lines_factory_for_job(job)
            with self.assertRaises(ValueError):
                list(source_factory())

    def test_legacy_single_file_sha256_is_upgraded_after_content_match(self):
        line = (
            b'https://legacy-manifest.example/login|legacy-user|'
            b'synthetic-legacy-secret|192.0.2.46\n'
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = Path(tmpdir) / 'legacy-source.txt'
            source_path.write_bytes(line)
            legacy_digest = hashlib.sha256(line).hexdigest()
            job = create_import_job(
                source_name='legacy-source.txt',
                source_path=str(source_path),
                source_size=len(line),
                source_hash=legacy_digest,
            )

            durable_import_file(str(source_path), job=job, batch_rows=10)

            job.refresh_from_db()
            self.assertNotEqual(job.source_hash, legacy_digest)
            self.assertEqual(job.source_size, len(line))
            self.assertEqual(job.status, 'completed')
            self.assertEqual(job.records.filter(status='committed').count(), 1)

    def test_path_streaming_rejects_a_source_edit_before_reading_changed_lines(self):
        first_line = (
            b'https://stream-change.example/login|first-user|'
            b'synthetic-first-secret|192.0.2.47\n'
        )
        old_second_line = (
            b'https://stream-change.example/login|second-user|'
            b'synthetic-before-2|192.0.2.48\n'
        )
        changed_second_line = (
            b'https://stream-change.example/login|second-user|'
            b'synthetic-after--2|192.0.2.48\n'
        )
        self.assertEqual(len(old_second_line), len(changed_second_line))

        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = Path(tmpdir) / 'stream-source.txt'
            source_path.write_bytes(first_line + old_second_line)
            mutation = {'done': False}

            def edit_after_first_commit(position):
                if position == 1 and not mutation['done']:
                    source_path.write_bytes(first_line + changed_second_line)
                    mutation['done'] = True

            job = durable_import_file(
                str(source_path), batch_rows=1,
                on_progress=edit_after_first_commit,
            )

            job.refresh_from_db()
            self.assertTrue(mutation['done'])
            self.assertEqual(job.status, 'failed')
            self.assertEqual(job.records.filter(status='committed').count(), 1)
            self.assertTrue(Account.objects.filter(
                user__name='first-user',
            ).exists())
            self.assertFalse(Account.objects.filter(
                user__name='second-user',
            ).exists())

    def test_path_streaming_rejects_new_files_added_mid_import(self):
        source_line = (
            b'https://stream-membership.example/login|existing-user|'
            b'synthetic-existing-secret|192.0.2.49\n'
        )
        added_line = (
            b'https://stream-membership.example/login|added-user|'
            b'synthetic-added-secret|192.0.2.50\n'
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / 'sources'
            source_dir.mkdir()
            (source_dir / 'first.txt').write_bytes(source_line)
            added = {'done': False}

            def add_file_after_first_commit(position):
                if position == 1 and not added['done']:
                    (source_dir / 'later.txt').write_bytes(added_line)
                    added['done'] = True

            job = durable_import_path(
                str(source_dir), ext='*', batch_rows=1,
                on_progress=add_file_after_first_commit,
            )

            job.refresh_from_db()
            self.assertTrue(added['done'])
            self.assertEqual(job.status, 'failed')
            self.assertEqual(job.records.filter(status='committed').count(), 1)
            self.assertTrue(Account.objects.filter(
                user__name='existing-user',
            ).exists())
            self.assertFalse(Account.objects.filter(
                user__name='added-user',
            ).exists())

    def test_new_ledger_entries_do_not_store_or_render_raw_source_lines(self):
        job = create_import_job(source_name='synthetic-private.txt')
        DurableImportSession(job, batch_rows=10, workers=1).run([
            b'https://private-audit.example/login|audit-user@private-audit.example|'
            b'synthetic-secret-never-rendered|192.0.2.43\n'
        ])
        record = job.records.get()
        self.assertEqual(record.source_line, '')

        administrator = get_user_model().objects.create_user(
            username='audit-admin', password='test-password', is_staff=True,
        )
        client = Client()
        client.force_login(administrator)
        response = client.get(reverse(
            'vault:import_job_records', args=[job.job_id],
        ))
        self.assertNotContains(response, 'synthetic-secret-never-rendered')


class RecordSortAuditTests(TestCase):
    def test_quick_search_sort_is_applied_before_page_selection(self):
        rows = []
        for number, path_token in enumerate(('zeta', 'alpha', 'mu'), start=1):
            rows.append(Account.objects.create(
                url=Url.objects.create(
                    name=f'https://sort-audit.example/{path_token}/login'
                ),
                account_type='user',
                user=User.objects.create(name=f'sort-user-{number}'),
                password=Pass.objects.create(name=f'sort-secret-{number}'),
            ))

        ascending = list(filter_accounts({
            'q': 'sort-audit.example', 'sort': 'url',
        }).values_list('url__name', flat=True))
        descending = list(filter_accounts({
            'q': 'sort-audit.example', 'sort': '-url',
        }).values_list('url__name', flat=True))

        self.assertEqual(ascending, sorted(row.url.name for row in rows))
        self.assertEqual(descending, sorted(
            (row.url.name for row in rows), reverse=True,
        ))

        request = RequestFactory().get('/search/', {
            'q': 'sort-audit.example', 'sort': 'url', 'page': '1',
        })
        first_page = _paginate_accounts(
            request, filter_accounts(request.GET), per_page=2,
        )
        self.assertEqual(
            [row.url.name for row in first_page.object_list],
            sorted(row.url.name for row in rows)[:2],
        )


class ImportFailureReportingAuditTests(TestCase):
    def setUp(self):
        administrator = get_user_model().objects.create_user(
            username='import-report-admin', password='test-password',
            is_staff=True,
        )
        self.client.force_login(administrator)

    def test_non_streaming_import_failure_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = Path(tmpdir) / 'empty-source.txt'
            source_path.write_bytes(b'')
            job = create_import_job(
                source_name='synthetic-failed-job.txt',
                source_path=str(source_path),
            )
            job.mark_failed('Synthetic failure for reporting test.')

            with mock.patch(
                'vault.import_engine.durable_import_path',
                return_value=job,
            ):
                response = self.client.post(reverse('vault:import'), {
                    'folder_path': str(source_path),
                }, follow=True)

        self.assertEqual(response.status_code, 200)
        messages = list(response.context['messages'])
        self.assertTrue(any(
            'did not complete' in str(message).lower()
            for message in messages
        ))
        self.assertFalse(any(
            'successfully imported' in str(message).lower()
            for message in messages
        ))


class AdvancedFilteringAuditTests(TestCase):
    def setUp(self):
        administrator = get_user_model().objects.create_user(
            username='filter-audit-admin', password='test-password',
            is_staff=True,
        )
        self.client.force_login(administrator)

    def test_invalid_date_filter_shows_form_error_without_server_failure(self):
        response = self.client.get(reverse('vault:advanced_search'), {
            'created_from': 'not-a-date',
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn('created_from', response.context['form'].errors)
        self.assertEqual(response.context['total'], 0)

    def test_invalid_date_export_does_not_become_an_unfiltered_export(self):
        response = self.client.get(reverse('vault:export_json'), {
            'adv': '1', 'created_from': 'not-a-date',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'[]')


class RealProcessCrashAuditTests(TransactionTestCase):
    reset_sequences = True

    def test_subprocess_death_after_commit_leaves_recoverable_durable_batch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = Path(tmpdir) / 'crash-source.txt'
            source_path.write_bytes(
                b'https://process-crash.example/login|crash-user|'
                b'synthetic-crash-secret|192.0.2.44\n'
                b'https://process-crash.example/login|tail-user|'
                b'synthetic-tail-secret|192.0.2.45\n'
            )
            job = create_import_job(
                source_name='synthetic-process-crash.txt',
                source_path=str(source_path),
            )

            child_code = r'''\
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "admin_LogIntel.settings")
import django
django.setup()
import vault.import_engine as engine
from vault.models import ImportJob
job = ImportJob.objects.get(job_id=os.environ["AUDIT_JOB_ID"])
real_flush = engine._durable_flush_batch
def commit_then_exit(*args, **kwargs):
    real_flush(*args, **kwargs)
    os._exit(73)
engine._durable_flush_batch = commit_then_exit
engine.durable_import_file(
    os.environ["AUDIT_SOURCE_PATH"], job=job, batch_rows=1,
)
os._exit(0)
'''
            env = os.environ.copy()
            env.update({
                'DJANGO_SETTINGS_MODULE': 'admin_LogIntel.settings',
                'POSTGRES_DB': str(connection.settings_dict['NAME']),
                'POSTGRES_USER': str(connection.settings_dict['USER'] or ''),
                'POSTGRES_PASSWORD': str(connection.settings_dict['PASSWORD'] or ''),
                'POSTGRES_HOST': str(connection.settings_dict['HOST'] or ''),
                'POSTGRES_PORT': str(connection.settings_dict['PORT'] or '5432'),
                'AUDIT_JOB_ID': str(job.job_id),
                'AUDIT_SOURCE_PATH': str(source_path),
            })
            completed = subprocess.run(
                [sys.executable, '-c', child_code],
                cwd=os.getcwd(),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )

            self.assertEqual(
                completed.returncode, 73,
                msg=f'crash helper failed (exit {completed.returncode})',
            )
            job.refresh_from_db()
            self.assertEqual(job.status, 'processing')
            self.assertEqual(job.records.filter(status='committed').count(), 1)
            self.assertEqual(Account.objects.count(), 1)

            ImportJob.objects.filter(pk=job.pk).update(
                lease_expires_at=timezone.now() - timezone.timedelta(seconds=1)
            )
            recovered = recover_job(
                ImportJob.objects.get(pk=job.pk),
                lines_iterable_factory=lines_factory_for_job(job),
            )

            job.refresh_from_db()
            self.assertIn(recovered.outcome, ('success', 'partial'))
            self.assertEqual(job.status, 'completed')
            self.assertEqual(job.total_committed, 2)
            self.assertEqual(Account.objects.count(), 2)
            self.assertEqual(job.records.count(), 2)
