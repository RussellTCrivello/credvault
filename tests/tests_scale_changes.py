"""Regression tests for the scalability changes (indexes, fast mode, caches)."""

import os
import tempfile
from unittest import mock

from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase

from vault import importer
from vault.models import Account, Email, ImportJob, ImportRecord, Ip, Pass, Url
from vault.validators import (
    _normalize_host_cached, _normalize_host_uncached, normalize_host,
)


def index_names(table):
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT indexname FROM pg_indexes WHERE tablename = %s', [table])
        return {row[0] for row in cursor.fetchall()}


class LedgerIndexTests(TransactionTestCase):
    def test_redundant_indexes_are_gone_and_unique_ones_stay(self):
        ledger = index_names('vault_importrecord')
        self.assertNotIn('vault_importrec_idem_idx', ledger)
        self.assertFalse([n for n in ledger if n.startswith(
            'vault_importrecord_idempotency_key') and n.endswith('_like')])
        self.assertFalse([n for n in ledger if 'import_job_id' in n])
        self.assertFalse([n for n in ledger if '_status_bd22bf2d' in n])
        self.assertIn('vault_importrecord_idempotency_key_key', ledger)
        self.assertIn('vault_importrec_job_pos_idx', ledger)
        self.assertIn('vault_importrec_job_status_idx', ledger)
        accounts = index_names('vault_account')
        self.assertFalse([n for n in accounts if n.endswith('fingerprint_5bceda5e_like')])
        self.assertIn('vault_account_fingerprint_5bceda5e_uniq', accounts)

    def test_reference_like_indexes_are_gone_and_search_indexes_stay(self):
        for table in ('vault_url', 'vault_email', 'vault_user', 'vault_pass',
                      'vault_ip'):
            names = index_names(table)
            self.assertFalse([n for n in names if n.endswith('_like')], table)
            self.assertIn(f'{table}_name_key', names)
            self.assertIn(f'{table}_name_lower_idx', names)
            self.assertIn(f'{table}_name_trgm', names)
        self.assertIn('vault_url_name_lower_c_idx', index_names('vault_url'))

    def test_fast_mode_never_recreates_dropped_indexes(self):
        recreated = [name for name, _ in importer._FAST_MODE_INDEXES
                     if name.endswith('_like')]
        self.assertEqual(recreated, [])

    def test_uniqueness_is_still_enforced(self):
        url = Url.objects.create(name='https://uniq.example.com')
        email = Email.objects.create(name='u@uniq.example')
        password = Pass.objects.create(name='pw')
        Account.objects.create(url=url, account_type='email', email=email,
                               password=password)
        with self.assertRaises(Exception):
            Account.objects.create(url=url, account_type='email', email=email,
                                   password=password)
        job = ImportJob.objects.create(source_name='x')
        ImportRecord.objects.create(import_job=job, source_position=1,
                                    idempotency_key='k1', status='skipped')
        with self.assertRaises(IntegrityError), transaction.atomic():
            ImportRecord.objects.create(import_job=job, source_position=2,
                                        idempotency_key='k1', status='skipped')

    def test_job_scoped_ledger_queries_still_use_an_index(self):
        job = ImportJob.objects.create(source_name='plan')
        with connection.cursor() as cursor:
            cursor.execute('SET enable_seqscan = off')
            cursor.execute(
                'EXPLAIN SELECT 1 FROM vault_importrecord'
                ' WHERE import_job_id = %s', [job.pk])
            plan = '\n'.join(row[0] for row in cursor.fetchall())
            cursor.execute('RESET enable_seqscan')
        self.assertIn('Index', plan)


class FastModeScopeTests(TransactionTestCase):
    TRGM = {'vault_url_name_trgm', 'vault_email_name_trgm',
            'vault_user_name_trgm', 'vault_pass_name_trgm', 'vault_ip_name_trgm'}

    def _all_present(self):
        names = set()
        for table in ('vault_url', 'vault_email', 'vault_user', 'vault_pass',
                      'vault_ip', 'vault_account', 'vault_importrecord'):
            names |= index_names(table)
        return names

    def test_default_scope_suspends_only_the_trigram_indexes(self):
        self.assertEqual(importer.fast_mode_scope(), 'search')
        before = self._all_present()
        self.assertTrue(self.TRGM <= before)
        with importer.search_indexes_suspended():
            during = self._all_present()
            self.assertEqual(before - during, self.TRGM)
            # Read-critical indexes stay while the import runs.
            self.assertIn('vault_account_email_id_c46a575a', during)
            self.assertIn('vault_email_name_lower_idx', during)
            self.assertIn('vault_acct_created_id_idx', during)
        self.assertEqual(self._all_present(), before)

    def test_all_scope_is_opt_in_and_always_restored(self):
        before = self._all_present()
        with mock.patch.dict(os.environ, {'VAULT_FAST_MODE_SCOPE': 'all'}):
            with self.assertRaises(RuntimeError):
                with importer.search_indexes_suspended():
                    during = self._all_present()
                    self.assertLess(len(during), len(before) - 5)
                    raise RuntimeError('import failed')
        self.assertEqual(self._all_present(), before)

    def test_unknown_scope_falls_back_to_search(self):
        with mock.patch.dict(os.environ, {'VAULT_FAST_MODE_SCOPE': 'bogus'}):
            self.assertEqual(importer.fast_mode_scope(), 'search')


class HostCacheTests(TestCase):
    CASES = ['example.com', 'EXAMPLE.com.', 'sub.Example.co.uk', 'bücher.de',
             'localhost', '10.0.0.1', '[::1]', '[2001:DB8::1]', 'nodot', 'a..b.com',
             'x' * 300 + '.com', '-bad-.com', 'example.c', '', ' example.com ',
             'xn--bcher-kva.de', 'exa mple.com']

    def test_cached_matches_uncached_for_every_case(self):
        for case in self.CASES:
            with self.subTest(case=case):
                self.assertEqual(normalize_host(case),
                                 _normalize_host_uncached(case))
                self.assertEqual(normalize_host(case),
                                 _normalize_host_uncached(case))  # warm hit

    def test_bytes_and_none_bypass_the_cache(self):
        self.assertEqual(normalize_host(b'Example.COM'), 'example.com')
        self.assertIsNone(normalize_host(None))

    def test_cache_is_bounded(self):
        info = _normalize_host_cached.cache_info()
        self.assertEqual(info.maxsize, 4096)


class ReferenceCacheBoundTests(TestCase):
    def test_bound_empties_only_oversized_sets(self):
        from vault.import_engine import _bound_reference_cache
        seen = {'url': {'a', 'b', 'c'}, 'pass': {'x'}}
        _bound_reference_cache(seen, 2)
        self.assertEqual(seen, {'url': set(), 'pass': {'x'}})
        _bound_reference_cache(seen, 0)
        self.assertEqual(seen, {'url': set(), 'pass': set()})


class ReferenceCacheImportTests(TransactionTestCase):
    def test_tiny_cache_ceiling_still_imports_every_record_once(self):
        import tempfile
        from vault import import_engine
        lines = [
            f'https://h{i % 3}.example.com/p|user{i}@x.example|pw-{i}|N/A'
            for i in range(60)
        ] * 2  # every line twice: second copy must be a duplicate
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'a.txt')
            with open(path, 'w') as handle:
                handle.write('\n'.join(lines) + '\n')
            with mock.patch.object(import_engine, 'MAX_REFERENCE_CACHE_VALUES', 2):
                job = import_engine.durable_import_path(path, batch_rows=10)
        self.assertEqual(job.status, 'completed')
        self.assertEqual(Account.objects.count(), 60)
        self.assertEqual(job.total_committed, 60)
        self.assertEqual(job.total_skipped, 60)
        self.assertEqual(Url.objects.count(), 3)

    def test_failed_batch_clears_the_cache_before_the_error_propagates(self):
        from vault import import_engine
        captured = {}
        real_flush = import_engine._durable_flush_batch

        def failing_flush(rows, metas, job_pk, **kwargs):
            seen = kwargs['seen']
            seen['url'].add('https://rolled-back.example')
            captured['seen'] = seen
            raise RuntimeError('simulated transaction failure')

        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'a.txt')
            with open(path, 'w') as handle:
                handle.write('https://a.example.com|u@x.example|pw|N/A\n')
            with mock.patch.object(import_engine, '_durable_flush_batch',
                                   failing_flush):
                try:
                    import_engine.durable_import_path(path, batch_rows=1)
                except Exception:
                    pass
        self.assertEqual(captured['seen']['url'], set())
        self.assertIs(import_engine._durable_flush_batch, real_flush)


class InsertOrderTests(TransactionTestCase):
    """Concurrent imports must insert unique keys in one global order.

    Two transactions that insert the same unique keys in different orders can
    deadlock on each other's uncommitted index entries (observed with
    ``--parallel-files 4``: ``deadlock detected`` on ``vault_ip``).  Sorting the
    ``INSERT ... SELECT`` makes every transaction acquire the waits in the same
    direction, so no cycle can form.
    """

    def test_reference_and_account_inserts_are_ordered(self):
        import re
        from vault import import_engine
        statements = [sql for _table, sql in importer._REFERENCE_INSERTS]
        statements += [importer._ACCOUNT_INSERT,
                       import_engine._ACCOUNT_INSERT_RETURNING]
        for sql in statements:
            self.assertRegex(sql, r'ORDER BY .* ON CONFLICT',
                             sql[:60])
        source = open(import_engine.__file__, encoding='utf-8').read()
        inserts = re.findall(
            r"INSERT INTO vault_(?:url|pass|email|user|ip) \(name[^\n]*", source)
        self.assertGreaterEqual(len(inserts), 10)
        for line in inserts:
            self.assertIn('ORDER BY', line, line[:80])

    def test_overlapping_parallel_file_imports_do_not_deadlock(self):
        """Smoke test through the real ``--parallel-files`` path.

        Deadlocks depend on timing and data size. This case did NOT reproduce
        the bug when the ordering was removed (3 of 3 runs passed), so it only
        guards the path; the structural test above is the real guard. The bug
        itself was reproduced by the opt-in ``FileConcurrencyBenchmarkTests``
        (``--parallel-files 4`` on four distinct 50k-line files).
        """
        import io
        import random
        import tempfile
        from django.core.management import call_command

        rng = random.Random(7)
        shared = [(f'https://h{i}.example.com/p', f'user{i}@x.example',
                   f'pw{i}', f'10.0.{i // 250}.{i % 250}') for i in range(30000)]
        with tempfile.TemporaryDirectory() as directory:
            for n in range(4):
                rows = rng.sample(shared, 20000)
                with open(os.path.join(directory, f'f{n}.txt'), 'w') as handle:
                    handle.write('\n'.join('|'.join(r) for r in rows) + '\n')
            call_command('importfolder', directory, parallel_files=4,
                         quiet=True, stdout=io.StringIO(), stderr=io.StringIO())
        jobs = list(ImportJob.objects.all())
        self.assertEqual(len(jobs), 4)
        self.assertTrue(all(job.status == 'completed' for job in jobs))
        self.assertEqual(Account.objects.count(), Ip.objects.count())
        self.assertGreater(Account.objects.count(), 25000)


class StaleStatisticsTests(TransactionTestCase):
    """Tables analyzed while empty must not wreck the account INSERT's plan."""

    def test_empty_table_statistics_are_refreshed_once_the_table_grows(self):
        with connection.cursor() as cursor:
            # Reproduce: analyzed with zero live tuples, then grown.
            cursor.execute(
                "INSERT INTO vault_ip (name, created_at, updated_at)"
                " SELECT 'old' || i, now(), now() FROM generate_series(1, 20) i")
            cursor.execute('DELETE FROM vault_ip')
            cursor.execute('ANALYZE vault_ip')  # 1 page, 0 live tuples
            cursor.execute(
                "SELECT reltuples FROM pg_class WHERE relname = 'vault_ip'")
            self.assertEqual(cursor.fetchone()[0], 0)
            with transaction.atomic():
                cursor.execute(
                    "INSERT INTO vault_ip (name, created_at, updated_at)"
                    " SELECT 'ip' || i, now(), now()"
                    " FROM generate_series(1, 30000) i")
                cursor.execute('EXPLAIN SELECT * FROM vault_ip')
                before = cursor.fetchone()[0]
                stale = importer.refresh_stale_reference_stats(cursor)
                cursor.execute('EXPLAIN SELECT * FROM vault_ip')
                after = cursor.fetchone()[0]
            self.assertIn('vault_ip', stale)
            self.assertIn('rows=1 ', before)
            self.assertNotIn('rows=1 ', after)

    def test_healthy_tables_are_left_alone(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO vault_url (name, created_at, updated_at)"
                " SELECT 'https://h' || i || '.example', now(), now()"
                " FROM generate_series(1, 30000) i")
            cursor.execute('VACUUM (ANALYZE) vault_url')
            with transaction.atomic():
                stale = importer.refresh_stale_reference_stats(cursor)
            self.assertNotIn('vault_url', stale)
            self.assertEqual(stale, [])


class StaleAccountStatisticsTests(TransactionTestCase):
    """The ledger's foreign key to ``vault_account`` is checked at COMMIT.

    With the account table's statistics saying "0 rows", PostgreSQL planned
    that check on the (created_at DESC, id) index, which cannot be searched by
    ``id``, and scanned the whole index once per ledger row: a 250,000-line
    import did not finish in 10 minutes (30 s with healthy statistics).
    """

    RI_QUERY = (
        "PREPARE ri_check(bigint) AS SELECT 1 FROM ONLY public.vault_account x"
        " WHERE id OPERATOR(pg_catalog.=) $1 FOR KEY SHARE OF x")

    def _make_account_stats_stale(self):
        """Analyzed with zero live tuples after a purge: 1 page, 0 tuples."""
        from vault.models import Account, ImportJob, ImportRecord
        with tempfile.TemporaryDirectory() as folder:
            self._write_lines(folder, 50, prefix='seed')
            call_command('importfolder', folder, quiet=True)
        ImportRecord.objects.all().delete()
        ImportJob.objects.all().delete()
        Account.objects.all().delete()
        with connection.cursor() as cursor:
            cursor.execute('ANALYZE vault_account')
            cursor.execute(
                "SELECT relpages, reltuples FROM pg_class"
                " WHERE relname = 'vault_account'")
            pages, tuples = cursor.fetchone()
        self.assertEqual(tuples, 0)
        self.assertGreaterEqual(pages, 1)

    @staticmethod
    def _write_lines(folder, count, prefix='row'):
        path = os.path.join(folder, f'{prefix}.txt')
        with open(path, 'w') as handle:
            for i in range(count):
                handle.write(
                    f'https://{prefix}{i}.example.com|user{prefix}{i}|pw{i}\n')

    def _account_stats(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT reltuples FROM pg_class"
                " WHERE relname = 'vault_account'")
            return cursor.fetchone()[0]

    def _ri_plan(self):
        with connection.cursor() as cursor:
            cursor.execute('DEALLOCATE ALL')
            cursor.execute(self.RI_QUERY)
            cursor.execute('SET plan_cache_mode = force_generic_plan')
            cursor.execute('EXPLAIN EXECUTE ri_check(5)')
            plan = '\n'.join(row[0] for row in cursor.fetchall())
            cursor.execute('RESET plan_cache_mode')
            cursor.execute('DEALLOCATE ALL')
        return plan

    def test_guard_analyzes_a_stale_account_table_that_has_grown(self):
        self._make_account_stats_stale()
        with connection.cursor() as cursor:
            with transaction.atomic():
                cursor.execute(
                    "INSERT INTO vault_url (name, created_at, updated_at)"
                    " VALUES ('https://x.example', now(), now())")
                cursor.execute(
                    "INSERT INTO vault_user (name, created_at, updated_at)"
                    " VALUES ('u', now(), now())")
                cursor.execute(
                    "INSERT INTO vault_pass (name, created_at, updated_at)"
                    " VALUES ('p', now(), now())")
                cursor.execute(
                    "INSERT INTO vault_account (url_id, account_type, user_id,"
                    " password_id, fingerprint, created_at, updated_at)"
                    " SELECT (SELECT id FROM vault_url LIMIT 1), 'username',"
                    " (SELECT id FROM vault_user LIMIT 1),"
                    " (SELECT id FROM vault_pass LIMIT 1),"
                    " md5(i::text) || md5(i::text), now(), now()"
                    " FROM generate_series(1, 30000) i")
                analyzed = importer.refresh_stale_account_stats(cursor)
        self.assertEqual(analyzed, ['vault_account'])
        self.assertGreater(self._account_stats(), 0)
        self.assertIn('vault_account_pkey', self._ri_plan())

    def test_healthy_account_table_is_left_alone(self):
        from vault.models import Account
        with tempfile.TemporaryDirectory() as folder:
            self._write_lines(folder, 3000)
            call_command('importfolder', folder, quiet=True)
        with connection.cursor() as cursor:
            cursor.execute('ANALYZE vault_account')
            with transaction.atomic():
                self.assertEqual(
                    importer.refresh_stale_account_stats(cursor), [])
        self.assertEqual(Account.objects.count(), 3000)

    def test_import_into_stale_account_table_refreshes_its_statistics(self):
        from vault.models import Account
        self._make_account_stats_stale()
        with tempfile.TemporaryDirectory() as folder:
            self._write_lines(folder, 3000)
            call_command('importfolder', folder, quiet=True)
        self.assertEqual(Account.objects.count(), 3000)
        self.assertGreater(self._account_stats(), 0)
        self.assertIn('vault_account_pkey', self._ri_plan())

    def test_only_known_tables_can_be_analyzed(self):
        with connection.cursor() as cursor:
            with self.assertRaises(ValueError):
                importer.refresh_stale_reference_stats(
                    cursor, ['auth_user; DROP TABLE vault_account'])
