"""Import metrics: aggregation, log format, secrecy and engine wiring."""

import logging
import os
import shutil
import tempfile
from unittest import mock

from django.test import TestCase, TransactionTestCase

from . import import_metrics
from .import_engine import durable_import_file
from .import_metrics import ImportMetrics
from .models import Account


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class ImportMetricsTests(TestCase):
    def test_snapshot_aggregates_batches(self):
        clock = FakeClock()
        metrics = ImportMetrics('job-1', interval=0, clock=clock)
        clock.now += 10
        metrics.record_batch(1000, 900, 2.0, 5000)
        metrics.record_batch(500, 500, 3.0, 2500)
        snap = metrics.snapshot()
        self.assertEqual(snap['batches'], 2)
        self.assertEqual(snap['items'], 1500)
        self.assertEqual(snap['valid_rows'], 1400)
        self.assertEqual(snap['items_per_second'], 150.0)
        self.assertEqual(snap['db_seconds'], 5.0)
        self.assertEqual(snap['db_busy_ratio'], 0.5)
        self.assertEqual(snap['batch_seconds_max'], 3.0)
        self.assertEqual(snap['batch_bytes'], 7500)

    def test_backlog_gauge_tracks_peak(self):
        metrics = ImportMetrics('j', interval=0)
        for value in (10, 500, 20):
            metrics.set_parse_backlog(value)
        snap = metrics.snapshot()
        self.assertEqual(snap['parse_pending_bytes'], 20)
        self.assertEqual(snap['parse_pending_bytes_max'], 500)

    def test_latency_samples_are_bounded(self):
        metrics = ImportMetrics('j', interval=0)
        for _ in range(import_metrics._LATENCY_SAMPLES * 3):
            metrics.record_batch(1, 1, 0.001)
        self.assertEqual(len(metrics._recent), import_metrics._LATENCY_SAMPLES)

    def test_periodic_line_respects_interval_and_zero_disables(self):
        clock = FakeClock()
        metrics = ImportMetrics('j', interval=30, clock=clock)
        with self.assertLogs('vault.import.metrics', 'INFO') as logs:
            metrics.record_batch(1, 1, 0.1)       # too early
            clock.now += 31
            metrics.record_batch(1, 1, 0.1)       # logs
            metrics.record_batch(1, 1, 0.1)       # too early again
        self.assertEqual(len(logs.records), 1)
        self.assertIn('event=progress', logs.output[0])
        silent = ImportMetrics('j', interval=0, clock=clock)
        with self.assertRaises(AssertionError):
            with self.assertLogs('vault.import.metrics', 'INFO'):
                clock.now += 1000
                silent.record_batch(1, 1, 0.1)
                silent.log_summary()

    def test_interval_environment(self):
        for raw, expected in (('', 30.0), ('abc', 30.0), ('0', 0.0),
                              ('-3', 0.0), ('5', 5.0), ('99999', 3600.0)):
            with mock.patch.dict(os.environ,
                                 {'VAULT_IMPORT_METRICS_INTERVAL': raw}):
                self.assertEqual(import_metrics.metrics_interval(), expected)

    def test_logging_failure_never_reaches_the_import(self):
        metrics = ImportMetrics('j', interval=1e-9)
        with mock.patch.object(import_metrics.logger, 'info',
                               side_effect=RuntimeError('log sink down')):
            metrics.record_batch(1, 1, 0.1)
            metrics.log_summary()


class EngineMetricsTests(TransactionTestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_import_records_metrics_without_leaking_values(self):
        secret = 'Sup3r-Secret-Pw-9137'
        path = os.path.join(self.directory, 'metrics.txt')
        with open(path, 'w', encoding='utf-8') as handle:
            for index in range(25):
                handle.write(
                    f'https://m{index}.example.com/login|user{index}@m.example'
                    f'|{secret}{index}|203.0.113.{index + 1}\n')
        with self.assertLogs('vault.import.metrics', 'INFO') as logs, \
                mock.patch.dict(os.environ,
                                {'VAULT_IMPORT_METRICS_INTERVAL': '3600'}):
            job = durable_import_file(path, batch_rows=10)
        self.assertEqual(Account.objects.count(), 25)
        summary = [line for line in logs.output if 'event=summary' in line]
        self.assertEqual(len(summary), 1)
        self.assertIn(' items=25 ', summary[0] + ' ')
        self.assertIn(' valid_rows=25 ', summary[0] + ' ')
        self.assertIn(' batches=3 ', summary[0] + ' ')
        joined = '\n'.join(logs.output)
        for forbidden in (secret, 'example.com', 'user1@', '203.0.113'):
            self.assertNotIn(forbidden, joined)
        self.assertIn(str(job.job_id), joined)
