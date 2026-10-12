"""
Performance and bulk-load checks.

Everything here runs in the normal test run except
:class:`ImportThroughputTests`, which writes hundreds of thousands of rows and
is therefore skipped unless ``VAULT_RUN_PERFORMANCE=1``: it measures both the
compatibility bulk loader and the durable streaming path on the machine it runs
on (see README.md, "Import speed").
"""

import os
import tempfile
import time
import unittest

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
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
from .models import Account, Email, Ip, Pass, Url, User
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
    knobs being compared (README.md, "Tuning for a fast machine").
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
