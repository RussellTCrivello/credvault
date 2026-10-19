"""
Performance and bulk-load checks.

Correctness checks run in the normal test suite. The opt-in throughput and
concurrent-search benchmarks write large synthetic datasets and require
``VAULT_RUN_PERFORMANCE=1`` or ``VAULT_RUN_CONCURRENT_PERF=1``. They measure
both the compatibility bulk loader and the durable path on the machine they
run on (see docs/README.md, "Import speed" and "Search during imports").
"""

import io
import math
import os
import statistics
import sys

try:
    import resource
except ImportError:  # pragma: no cover - unavailable on Windows
    resource = None
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.paginator import Paginator
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from .counts import count_label, exact_count
from .importer import (
    STREAM_BATCH_SIZE,
    BulkImportSession,
    bulk_import_lines,
    import_path,
    stage_rows,
    worker_count,
)
from .import_engine import _source_manifest, durable_import_path
from .models import Account, Email, ImportJob, Ip, Pass, Url, User
from .utils import parse_line

PASSWORD = 'correct-horse-battery'

#: Set VAULT_RUN_PERFORMANCE=1 to include the throughput measurement.
PERFORMANCE_ENABLED = os.environ.get('VAULT_RUN_PERFORMANCE') == '1'
DEFAULT_PERF_LINES = 200000


class BulkLoadMatchesOrmTests(TestCase):
    """Records written by the SQL loader must be identical to ORM records."""

    def setUp(self):
        get_user_model().objects.create_superuser('admin', 'a@example.com', PASSWORD)
        self.client.login(username='admin', password=PASSWORD)

    def test_bulk_loaded_records_have_the_orm_fingerprint(self):
        lines = [
            'https://example.com/login|person@example.com|Secret 1|203.0.113.10',
            'https://example.com/login|bob|Secret 2|N/A',
            'https://Example.com/login|Person@Example.com|Secret 1|203.0.113.10',
        ]
        created, skipped = bulk_import_lines(parse_line(x) for x in lines)
        self.assertEqual((created, skipped), (2, 0))  # duplicate spelling dropped

        for account in Account.objects.all():
            self.assertEqual(account.compute_fingerprint(), account.fingerprint)

    def test_orm_refuses_a_record_the_loader_already_stored(self):
        bulk_import_lines(iter([
            parse_line('https://example.com/login|person@example.com|Secret 1'),
        ]))
        account = Account.objects.get()
        duplicate = Account(
            url=account.url,
            account_type=account.account_type,
            email=account.email,
            password=account.password,
        )
        with self.assertRaises(ValidationError):
            duplicate.save()

    def test_second_import_of_the_same_file_adds_nothing(self):
        line = 'https://example.com/a|user1|pw1|1.2.3.4'
        first, _ = bulk_import_lines(iter([parse_line(line)]))
        second, _ = bulk_import_lines(iter([parse_line(line)]))
        self.assertEqual((first, second), (1, 0))
        self.assertEqual(Account.objects.count(), 1)
        self.assertEqual(Url.objects.count(), 1)
        self.assertEqual(Pass.objects.count(), 1)

    def test_reference_rows_are_not_duplicated_across_batches(self):
        lines = [
            f'https://site{i % 3}.example.com/x|user{i}|pw{i % 2}|1.2.3.{i % 5}'
            for i in range(40)
        ]
        bulk_import_lines(parse_line(x) for x in lines)
        self.assertEqual(Account.objects.count(), 40)
        self.assertEqual(Url.objects.count(), 3)
        self.assertEqual(Pass.objects.count(), 2)
        self.assertEqual(User.objects.count(), 40)
        self.assertEqual(Email.objects.count(), 0)


class ExactCountTests(TestCase):
    def setUp(self):
        # Records go through the loader, the real write path for bulk data.
        bulk_import_lines(
            parse_line(f'https://example.com/login|user{i}|pw{i}')
            for i in range(25)
        )

    def test_count_is_exact_below_ten_thousand(self):
        self.assertEqual(exact_count(Account.objects.all()), 25)

    def test_count_labels_are_exact_and_formatted(self):
        self.assertEqual(count_label(10000), '10,000')
        self.assertEqual(count_label(10001), '10,001')
        self.assertEqual(count_label(42), '42')

    def test_paginator_uses_the_full_queryset_count(self):
        paginator = Paginator(Account.objects.order_by('id'), per_page=10)
        self.assertEqual(paginator.count, 25)
        self.assertEqual(paginator.num_pages, 3)
        self.assertEqual(paginator.page(3).start_index(), 21)

    def test_search_shows_exact_total_and_reaches_page_beyond_ten_thousand(self):
        total = 10005
        bulk_import_lines(
            parse_line(
                f'https://large-results.example/login|large-user{i}|'
                f'large-secret{i}'
            )
            for i in range(total)
        )
        get_user_model().objects.create_superuser(
            'admin', 'a@example.com', PASSWORD,
        )
        self.client.login(username='admin', password=PASSWORD)

        response = self.client.get(reverse('vault:search'), {
            'field': 'url',
            'q': 'https://large-results.example/login',
            'page': '101',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total'], total)
        self.assertEqual(response.context['page_obj'].number, 101)
        self.assertEqual(response.context['page_obj'].paginator.num_pages, 101)
        self.assertEqual(len(response.context['records']), 5)
        self.assertContains(response, '10,005 matching records')
        self.assertNotContains(response, '10,000+')


class ReferenceUsageTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_superuser('admin', 'a@example.com', PASSWORD)
        self.client.login(username='admin', password=PASSWORD)
        bulk_import_lines(
            parse_line(f'https://a.example.com/x|user{i}|pw|1.2.3.4')
            for i in range(3)
        )
        bulk_import_lines(iter([parse_line('https://b.example.com/x|user9|pw|1.2.3.4')]))

    def test_usage_column_counts_records_per_value(self):
        from .views import _attach_usage
        urls = list(Url.objects.order_by('name'))
        _attach_usage('url', urls)
        usage = {u.name: u.usage for u in urls}
        self.assertEqual(usage, {
            'https://a.example.com/x': 3,
            'https://b.example.com/x': 1,
        })

    def test_reference_page_renders_usage(self):
        response = self.client.get(reverse('vault:references', args=['url']))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'https://a.example.com/x')


class PostgresLimitTests(TestCase):
    """Values PostgreSQL cannot index must be refused, not crash a batch."""

    def test_long_multibyte_url_is_skipped_and_batch_still_loads(self):
        long_url = 'https://example.com/' + ''.join(
            chr(0x4E00 + (i * 7919) % 3000) for i in range(1000)
        )
        created, skipped = bulk_import_lines(iter([
            parse_line(f'{long_url}|user1|pw1'),
            parse_line('https://example.com/ok|user2|pw2'),
        ]))
        self.assertEqual((created, skipped), (1, 1))
        self.assertEqual(Url.objects.count(), 1)

    def test_long_ascii_url_within_the_limit_is_stored(self):
        url = 'https://example.com/' + 'a' * 1900
        created, skipped = bulk_import_lines(iter([parse_line(f'{url}|user1|pw1')]))
        self.assertEqual((created, skipped), (1, 0))


class CopyConversionTests(TestCase):
    def test_boolean_and_datetime_conversion(self):
        import datetime as dt
        from vault.management.commands.copy_from_sqlite import converter_for

        self.assertIs(converter_for('boolean')(1), True)
        self.assertIs(converter_for('boolean')(0), False)
        self.assertIsNone(converter_for('boolean')(None))

        aware = converter_for('timestamp with time zone')('2026-10-11 18:04:39.610975')
        self.assertEqual(aware.tzinfo, dt.timezone.utc)
        self.assertEqual(aware.microsecond, 610975)

        naive = converter_for('timestamp without time zone')('2026-10-11 18:04:39')
        self.assertIsNone(naive.tzinfo)

        self.assertEqual(
            converter_for('date')('2026-10-11'), dt.date(2026, 10, 11)
        )
        self.assertEqual(converter_for('bigint')(42), 42)

    def test_copy_refuses_a_source_missing_required_columns(self):
        """
        A SQLite file that predates the current schema must fail with a
        clean CommandError naming the missing column(s), not a raw
        NotNullViolation from the COPY, and nothing may be saved.
        """
        import sqlite3
        from django.core.management import call_command
        from django.core.management.base import CommandError

        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, 'old-schema.db')
        try:
            source = sqlite3.connect(path)
            source.executescript(
                'CREATE TABLE vault_url ('
                'id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,'
                ' created_at TEXT);'
            )
            source.execute(
                "INSERT INTO vault_url VALUES "
                "(1, 'https://old.example/login', '2025-05-01 12:00:00')"
            )
            source.commit()
            source.close()

            with self.assertRaises(CommandError) as raised:
                call_command('copy_from_sqlite', path)
            self.assertIn('updated_at', str(raised.exception))

            # The failed copy saved nothing.
            self.assertEqual(Url.objects.count(), 0)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


def synthetic_line(index):
    """One valid record, mostly unique so the insert work is real."""
    return (
        f'https://host{index % 50000}.example.com/login/{index}'
        f'|user{index}@perf.example|Secret-{index}'
        f'|10.{(index // 250) % 250}.{index % 250}.{index % 250}'
    )


class ImportThroughputTests(TransactionTestCase):
    """
    Opt-in import measurement: lines per second on this machine.

    Run it on the machine you care about -- it uses the settings you export:

        VAULT_RUN_PERFORMANCE=1 VAULT_PERF_LINES=1000000 VAULT_PERF_FAST=1 \
            POSTGRES_PASSWORD='...' .venv/bin/python manage.py test \
            vault.tests_performance.ImportThroughputTests

    ``VAULT_PERF_LINES`` picks the size (default 200,000), ``VAULT_PERF_FAST=1``
    imports with the search indexes suspended, and
    ``VAULT_IMPORT_BATCH_ROWS`` / ``VAULT_IMPORT_WORKERS`` are the importer
    knobs being compared (docs/README.md, "Tuning for a fast machine").
    """

    @unittest.skipUnless(
        PERFORMANCE_ENABLED,
        'set VAULT_RUN_PERFORMANCE=1 to run the throughput measurement',
    )
    def test_import_throughput(self):
        lines = int(os.environ.get('VAULT_PERF_LINES', DEFAULT_PERF_LINES))
        fast_mode = os.environ.get('VAULT_PERF_FAST') == '1'

        handle = tempfile.NamedTemporaryFile(
            'w', suffix='.txt', delete=False, encoding='utf-8'
        )
        path = handle.name
        try:
            for index in range(lines):
                handle.write(synthetic_line(index) + '\n')
            handle.close()

            started = time.time()
            created, skipped, files = import_path(
                path, ext='txt', fast_mode=fast_mode
            )
            elapsed = time.time() - started
        finally:
            handle.close()
            if os.path.exists(path):
                os.unlink(path)

        self.assertEqual(created + skipped, lines)
        self.assertEqual((files, skipped), (1, 0))
        self.assertEqual(Account.objects.count(), lines)
        for model in (Url, Email, Pass, Ip):
            self.assertGreater(model.objects.count(), 0)

        print(
            f'\nPerformance: {created:,} records in {elapsed:.1f}s '
            f'({created / elapsed:,.0f} lines/s); fast mode '
            f'{"on" if fast_mode else "off"}; batch '
            f'{stage_rows():,} rows; {worker_count()} parse worker(s)'
        )

    @unittest.skipUnless(
        PERFORMANCE_ENABLED,
        'set VAULT_RUN_PERFORMANCE=1 to run the throughput measurement',
    )
    def test_durable_path_import_throughput(self):
        lines = int(os.environ.get('VAULT_PERF_LINES', DEFAULT_PERF_LINES))
        handle = tempfile.NamedTemporaryFile(
            'wb', suffix='.txt', delete=False,
        )
        path = handle.name
        try:
            for index in range(lines):
                handle.write((synthetic_line(index) + '\n').encode())
            handle.close()

            started = time.perf_counter()
            prepared_manifest = _source_manifest(path)
            job = durable_import_path(
                path,
                prepared_manifest=prepared_manifest,
                expected_line_count=lines,
                batch_rows=STREAM_BATCH_SIZE,
            )
            elapsed = time.perf_counter() - started
        finally:
            handle.close()
            if os.path.exists(path):
                os.unlink(path)

        self.assertEqual(job.status, 'completed')
        self.assertEqual(job.total_discovered, lines)
        self.assertEqual(job.total_committed, lines)
        self.assertEqual(Account.objects.count(), lines)
        print(
            f'\nDurable path performance: {job.total_committed:,} records '
            f'in {elapsed:.1f}s ({job.total_committed / elapsed:,.0f} lines/s); '
            f'{STREAM_BATCH_SIZE:,}-line durable batches'
        )

    @unittest.skipUnless(
        PERFORMANCE_ENABLED,
        'set VAULT_RUN_PERFORMANCE=1 to run the throughput measurement',
    )
    def test_bulk_session_and_orm_agree(self):
        # The same check as BulkLoadMatchesOrmTests, through the batching
        # session the importer now uses (workers included).
        session = BulkImportSession(workers=2, batch_rows=250)
        session.add_lines(
            (synthetic_line(index) + '\n').encode() for index in range(500)
        )
        created, skipped = session.finish()
        self.assertEqual((created, skipped), (500, 0))
        for account in Account.objects.all():
            self.assertEqual(account.compute_fingerprint(), account.fingerprint)



class ConcurrentImportSearchTests(TransactionTestCase):
    """End-to-end interactive search measurements during durable ingestion."""

    @unittest.skipUnless(
        os.environ.get('VAULT_RUN_CONCURRENT_PERF') == '1',
        'set VAULT_RUN_CONCURRENT_PERF=1 to measure searches during import',
    )
    def test_search_latency_and_storage_during_durable_import(self):
        from django.db import close_old_connections, connections

        line_count = max(
            10000, int(os.environ.get('VAULT_CONCURRENT_PERF_LINES', '100000')),
        )
        batch_rows = max(
            1000,
            int(os.environ.get('VAULT_CONCURRENT_PERF_BATCH_ROWS', '10000')),
        )
        interval = max(
            0.05,
            float(os.environ.get('VAULT_CONCURRENT_PERF_INTERVAL', '0.1')),
        )

        administrator = get_user_model().objects.create_superuser(
            'concurrent-perf-admin', 'perf@example.invalid', PASSWORD,
        )
        self.client.force_login(administrator)
        Account.objects.create(
            url=Url.objects.create(
                name='https://interactive-query.example/login',
            ),
            account_type='email',
            email=Email.objects.create(
                name='interactive-search@latency.example',
            ),
            password=Pass.objects.create(name='synthetic-query-secret'),
        )

        cases = (
            (
                'exact email', reverse('vault:search'),
                {'field': 'email', 'q': 'interactive-search@latency.example'},
            ),
            (
                'substring URL', reverse('vault:advanced_search'),
                {'url': 'interactive-query.example'},
            ),
        )
        baseline = {name: [] for name, _url, _params in cases}
        concurrent = {name: [] for name, _url, _params in cases}

        def measure(destination, name, url, params):
            started = time.perf_counter()
            response = self.client.get(url, params)
            elapsed = time.perf_counter() - started
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context['total'], 1)
            destination[name].append(elapsed)

        # Record a small in-process baseline immediately before ingestion.
        for _ in range(5):
            for name, url, params in cases:
                measure(baseline, name, url, params)

        with tempfile.TemporaryDirectory(prefix='credvault-search-load-') as directory:
            path = os.path.join(directory, 'concurrent-import.txt')
            with open(path, 'wb') as source:
                for index in range(line_count):
                    source.write((synthetic_line(index) + '\n').encode())

            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT pg_database_size(current_database()), wal_bytes '
                    'FROM pg_stat_wal'
                )
                size_before, wal_before = cursor.fetchone()

            result = {}

            def import_source():
                close_old_connections()
                started = time.perf_counter()
                try:
                    result['job'] = durable_import_path(
                        path,
                        batch_rows=batch_rows,
                        workers=1,
                    )
                    result['seconds'] = time.perf_counter() - started
                    return result['job']
                finally:
                    # Do not leave the test database pinned by the executor's
                    # thread-local Django connection.
                    connections.close_all()

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(import_source)
                while not future.done():
                    for name, url, params in cases:
                        measure(concurrent, name, url, params)
                    time.sleep(interval)
                job = future.result()

            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT pg_database_size(current_database()), wal_bytes '
                    'FROM pg_stat_wal'
                )
                size_after, wal_after = cursor.fetchone()
                cursor.execute(
                    'SELECT relname, pg_total_relation_size(relid) '
                    'FROM pg_stat_user_tables WHERE schemaname = %s '
                    'AND relname = ANY(%s) ORDER BY relname',
                    [
                        'public',
                        [
                            'vault_account', 'vault_url', 'vault_email',
                            'vault_user', 'vault_pass', 'vault_ip',
                            'vault_importrecord',
                        ],
                    ],
                )
                table_sizes = dict(cursor.fetchall())

        self.assertEqual(job.status, 'completed')
        self.assertEqual(job.total_committed, line_count)
        self.assertEqual(Account.objects.count(), line_count + 1)
        self.assertTrue(all(concurrent[name] for name, _url, _params in cases))

        def percentile(values, fraction):
            ordered = sorted(values)
            index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
            return ordered[index]

        process_peak_rss = 'unavailable'
        if resource is not None:
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            peak_bytes = rss if sys.platform == 'darwin' else rss * 1024
            process_peak_rss = f'{peak_bytes / (1024 ** 2):,.1f} MiB'

        print(
            f'\nConcurrent benchmark: {line_count:,} durable records in '
            f"{result['seconds']:.1f}s "
            f"({line_count / result['seconds']:,.0f} records/s), "
            f"{job.total_committed:,} committed across "
            f"{(line_count + batch_rows - 1) // batch_rows:,} batches; "
            f"database size delta {size_after - size_before:,} bytes; "
            f"WAL delta {wal_after - wal_before:,} bytes; "
            f"test-process peak RSS {process_peak_rss}"
        )
        for table, table_bytes in sorted(table_sizes.items()):
            print(f'  storage {table}: {table_bytes:,} bytes')
        for name, _url, _params in cases:
            before = baseline[name]
            during = concurrent[name]
            print(
                f'  {name}: baseline n={len(before)} '
                f'p50={statistics.median(before) * 1000:.1f}ms '
                f'p95={percentile(before, 0.95) * 1000:.1f}ms; '
                f'during n={len(during)} '
                f'p50={statistics.median(during) * 1000:.1f}ms '
                f'p95={percentile(during, 0.95) * 1000:.1f}ms '
                f'max={max(during) * 1000:.1f}ms'
            )



class FileConcurrencyBenchmarkTests(TransactionTestCase):
    """Compare sequential and bounded multi-file durable imports end to end."""

    @unittest.skipUnless(
        os.environ.get('VAULT_RUN_FILE_CONCURRENCY_PERF') == '1',
        'set VAULT_RUN_FILE_CONCURRENCY_PERF=1 to compare file concurrency',
    )
    def test_file_concurrency_scaling(self):
        line_count = max(
            10000, int(os.environ.get('VAULT_FILE_CONCURRENCY_LINES', '100000')),
        )
        file_count = max(
            2, min(8, int(os.environ.get('VAULT_FILE_CONCURRENCY_FILES', '4'))),
        )
        results = []

        def clear_imported_data():
            ImportJob.objects.all().delete()
            Account.objects.all().delete()
            for model in (Url, Email, User, Pass, Ip):
                model.objects.all().delete()

        with tempfile.TemporaryDirectory(prefix='credvault-file-scale-') as root:
            single_directory = os.path.join(root, 'sequential')
            many_directory = os.path.join(root, 'parallel')
            os.makedirs(single_directory)
            os.makedirs(many_directory)

            with open(
                os.path.join(single_directory, 'all.txt'),
                'w', encoding='utf-8',
            ) as handle:
                for index in range(line_count):
                    handle.write(synthetic_line(index) + '\n')

            for file_index in range(file_count):
                start = line_count * file_index // file_count
                stop = line_count * (file_index + 1) // file_count
                with open(
                    os.path.join(many_directory, f'part-{file_index:04}.txt'),
                    'w', encoding='utf-8',
                ) as handle:
                    for index in range(start, stop):
                        handle.write(synthetic_line(index) + '\n')

            for parallel_files in (1, 2, 4):
                clear_imported_data()
                path = single_directory if parallel_files == 1 else many_directory
                stdout = io.StringIO()
                stderr = io.StringIO()
                started = time.perf_counter()
                if parallel_files == 1:
                    # Keep the intra-file baseline reproducible on this host;
                    # parallel file runs use one parser per file by design.
                    with mock.patch.dict(
                        os.environ, {'VAULT_IMPORT_WORKERS': '2'},
                    ):
                        call_command(
                            'importfolder', path, parallel_files=1, quiet=True,
                            stdout=stdout, stderr=stderr,
                        )
                else:
                    call_command(
                        'importfolder', path, parallel_files=parallel_files,
                        quiet=True, stdout=stdout, stderr=stderr,
                    )
                elapsed = time.perf_counter() - started
                jobs = list(ImportJob.objects.order_by('source_path'))
                self.assertEqual(
                    sum(job.total_committed for job in jobs), line_count,
                )
                self.assertTrue(all(
                    job.status == 'completed' for job in jobs
                ))
                self.assertEqual(Account.objects.count(), line_count)
                expected_jobs = 1 if parallel_files == 1 else file_count
                self.assertEqual(len(jobs), expected_jobs)
                results.append((parallel_files, elapsed))

        print(f'\nFile-level durable import benchmark: {line_count:,} records')
        for parallel_files, elapsed in results:
            print(
                f'  --parallel-files={parallel_files}: {elapsed:.1f}s '
                f'({line_count / elapsed:,.0f} records/s)'
            )
