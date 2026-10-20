"""PostgreSQL settings advisory: read-only, once per process, never raises."""
import os
import tempfile
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TransactionTestCase

from vault import db_advisor


class AdvisoryRuleTests(SimpleTestCase):
    GB = 1024 ** 3
    MB = 1024 ** 2

    def test_stock_defaults_warn_and_name_the_settings(self):
        stock = {
            'shared_buffers': 128 * self.MB, 'max_wal_size': self.GB,
            'checkpoint_timeout': 300,
        }
        messages = db_advisor.advisories(stock)
        self.assertEqual(len(messages), 1)
        for text in ('shared_buffers=128 MB', 'max_wal_size=1 GB',
                     'VAULT_PG_ADVISORY=0'):
            self.assertIn(text, messages[0])
        self.assertNotIn('checkpoint_timeout=', messages[0])

    def test_default_checkpoint_timeout_alone_does_not_warn(self):
        self.assertEqual(db_advisor.advisories({
            'shared_buffers': 4 * self.GB, 'max_wal_size': 32 * self.GB,
            'checkpoint_timeout': 300,
        }), [])

    def test_tuned_settings_do_not_warn(self):
        tuned = {
            'shared_buffers': 4 * self.GB, 'max_wal_size': 32 * self.GB,
            'checkpoint_timeout': 1800,
        }
        self.assertEqual(db_advisor.advisories(tuned), [])

    def test_only_the_stock_settings_are_named(self):
        mixed = {
            'shared_buffers': 4 * self.GB, 'max_wal_size': self.GB,
            'checkpoint_timeout': 1800,
        }
        text = db_advisor.advisories(mixed)[0]
        self.assertIn('max_wal_size=1 GB', text)
        self.assertNotIn('shared_buffers=', text.split('. In one')[0])

    def test_missing_values_do_not_warn(self):
        self.assertEqual(db_advisor.advisories({}), [])

    def test_describe_formats_units(self):
        line = db_advisor.describe({
            'server_version': '18.6', 'shared_buffers': 4 * self.GB,
            'checkpoint_timeout': 1800, 'wal_buffers': -8192,
            'synchronous_commit': 'on',
        })
        self.assertIn('postgresql 18.6', line)
        self.assertIn('shared_buffers=4 GB', line)
        self.assertIn('checkpoint_timeout=30 min', line)
        self.assertIn('wal_buffers=auto', line)
        self.assertIn('synchronous_commit=on', line)

    def test_disable_switch(self):
        for value in ('0', 'false', 'OFF', 'no'):
            with mock.patch.dict(os.environ, {'VAULT_PG_ADVISORY': value}):
                self.assertFalse(db_advisor.advisory_enabled())
        with mock.patch.dict(os.environ, {'VAULT_PG_ADVISORY': '1'}):
            self.assertTrue(db_advisor.advisory_enabled())


class ReadSettingsTests(SimpleTestCase):
    databases = {'default'}

    def test_reads_real_server_values_with_units_applied(self):
        settings = db_advisor.read_pg_settings()
        self.assertGreaterEqual(settings['shared_buffers'], 128 * 1024)
        self.assertIsInstance(settings['shared_buffers'], int)
        self.assertGreater(settings['checkpoint_timeout'], 0)
        self.assertIn(settings['synchronous_commit'], ('on', 'off', 'local',
                                                       'remote_write',
                                                       'remote_apply'))

    def test_errors_are_swallowed(self):
        broken = mock.MagicMock()
        broken.vendor = 'postgresql'
        broken.cursor.side_effect = RuntimeError('boom')
        self.assertEqual(db_advisor.read_pg_settings(broken), {})

    def test_non_postgres_backend_is_ignored(self):
        other = mock.MagicMock()
        other.vendor = 'sqlite'
        self.assertEqual(db_advisor.read_pg_settings(other), {})
        other.cursor.assert_not_called()


class ReportOnceTests(SimpleTestCase):
    def setUp(self):
        db_advisor.reset_for_tests()
        self.addCleanup(db_advisor.reset_for_tests)

    def _stock(self):
        return {
            'server_version': '18.6', 'shared_buffers': 128 * 1024 ** 2,
            'max_wal_size': 1024 ** 3, 'checkpoint_timeout': 300,
            'synchronous_commit': 'on',
        }

    def test_logs_snapshot_and_warning_once(self):
        with mock.patch.object(db_advisor, 'read_pg_settings',
                               return_value=self._stock()):
            with self.assertLogs('vault.import.metrics', 'INFO') as logs:
                db_advisor.report_once()
                db_advisor.report_once()
        levels = [r.levelname for r in logs.records]
        self.assertEqual(levels, ['INFO', 'WARNING'])
        self.assertIn('pg_settings postgresql 18.6', logs.output[0])
        self.assertIn('pg_advisory', logs.output[1])

    def test_silenced_by_environment(self):
        with mock.patch.dict(os.environ, {'VAULT_PG_ADVISORY': '0'}):
            with mock.patch.object(db_advisor, 'read_pg_settings') as read:
                db_advisor.report_once()
        read.assert_not_called()

    def test_unreadable_settings_log_nothing_and_do_not_raise(self):
        with mock.patch.object(db_advisor, 'read_pg_settings',
                               return_value={}):
            with self.assertNoLogs('vault.import.metrics', 'INFO'):
                db_advisor.report_once()


class ImportIntegrationTests(TransactionTestCase):
    def setUp(self):
        db_advisor.reset_for_tests()
        self.addCleanup(db_advisor.reset_for_tests)

    def test_import_reports_settings_and_still_imports(self):
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, 'a.txt'), 'w') as handle:
                handle.write('https://one.example.com|alice|pw1\n')
                handle.write('\n')
                handle.write('https://two.example.com|bob|pw2|10.0.0.1\n')
            with self.assertLogs('vault.import.metrics', 'INFO') as logs:
                call_command('importfolder', folder, quiet=True)
        joined = '\n'.join(logs.output)
        self.assertIn('pg_settings postgresql', joined)
        from vault.models import Account
        self.assertEqual(Account.objects.count(), 2)
