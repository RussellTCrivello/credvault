"""Regression tests for the scalability changes (indexes, fast mode, caches)."""

import os
from unittest import mock

from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase

from . import importer
from .models import Account, Email, ImportJob, ImportRecord, Ip, Pass, Url
from .validators import (
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
        self.assertIn('vault_importrecord_idempotency_key_key', ledger)
        self.assertIn('vault_importrec_job_pos_idx', ledger)
        self.assertIn('vault_importrec_job_status_idx', ledger)
        accounts = index_names('vault_account')
        self.assertFalse([n for n in accounts if n.endswith('fingerprint_5bceda5e_like')])
        self.assertIn('vault_account_fingerprint_5bceda5e_uniq', accounts)

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
        from .import_engine import _bound_reference_cache
        seen = {'url': {'a', 'b', 'c'}, 'pass': {'x'}}
        _bound_reference_cache(seen, 2)
        self.assertEqual(seen, {'url': set(), 'pass': {'x'}})
        _bound_reference_cache(seen, 0)
        self.assertEqual(seen, {'url': set(), 'pass': set()})


class ReferenceCacheImportTests(TransactionTestCase):
    def test_tiny_cache_ceiling_still_imports_every_record_once(self):
        import tempfile
        from . import import_engine
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
        from . import import_engine
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
