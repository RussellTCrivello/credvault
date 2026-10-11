"""
Tests for the speed work: URL byte cap, import writers, capped counts, the
search API, the Settings page and import error handling.

Every test runs on SQLite and on PostgreSQL unless it is marked otherwise.
"""

import json
import os
import random
import shutil
import tempfile
from dataclasses import replace
from unittest import mock, skipUnless

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.urls import reverse

from .benchmarking import write_synthetic_file
from .db import (
    analyze_tables, capped_count, index_names, is_postgresql, table_count,
    text_collation_is_c,
)
from .importer import (
    Source, bulk_import_lines, import_sources, store_lines,
)
from .models import (
    Account, Email, Ip, Pass, PerformanceSettings, Url, User, record_fingerprint,
)
from .performance import IMPORT_PROFILES, QUERY_PROFILES, import_profile, query_profile
from .utils import parse_line
from .validators import MAX_URL_BYTES, normalize_url
from .views import CappedPaginator

ARABIC_LETTERS = 'ابتثجحخدذرزسشصضطظعغفقكلمنهوي'


def _admin_client(test_case):
    administrator = get_user_model().objects.create_user(
        username='performance-admin', password='test-password', is_staff=True,
    )
    test_case.client.force_login(administrator)
    return test_case.client


class LongUrlTests(TestCase):
    """A URL too long for a PostgreSQL index is refused as a format error."""

    def test_long_non_ascii_url_is_not_canonical(self):
        rnd = random.Random(11)
        # 2000 Arabic characters are 4000 UTF-8 bytes: above the index limit.
        url = 'https://example.com/' + ''.join(
            rnd.choice(ARABIC_LETTERS) for _ in range(2000)
        )
        self.assertGreater(len(url.encode('utf-8')), MAX_URL_BYTES)
        self.assertIsNone(normalize_url(url))

    def test_url_at_the_cap_is_still_accepted(self):
        path = 'a' * (MAX_URL_BYTES - len('https://example.com/'))
        url = 'https://example.com/' + path
        self.assertEqual(len(url.encode('utf-8')), MAX_URL_BYTES)
        self.assertEqual(normalize_url(url), url)

    def test_add_form_reports_long_url_as_a_format_error(self):
        client = _admin_client(self)
        rnd = random.Random(12)
        url = 'https://example.com/' + ''.join(
            rnd.choice(ARABIC_LETTERS) for _ in range(2000)
        )
        response = client.post(reverse('vault:add'), {
            'account_type': 'user',
            'url_name': url,
            'user_name': 'bob',
            'password_name': 'secret',
            'ip_name': '198.51.100.7',
        })
        self.assertEqual(response.status_code, 200)  # re-rendered, no 500
        self.assertContains(response, 'Enter a valid URL')
        self.assertEqual(Account.objects.count(), 0)
        self.assertEqual(Url.objects.count(), 0)

    def test_import_skips_long_url_line_and_keeps_the_rest(self):
        long_url = 'https://example.com/' + 'ع' * 2000
        lines = [
            f'{long_url}|bob|secret|',
            'https://example.com/ok|bob|secret|',
        ]
        created, skipped = bulk_import_lines(parse_line(line) for line in lines)
        self.assertEqual((created, skipped), (1, 1))
        self.assertEqual(Account.objects.count(), 1)


class ImportWriterTests(TestCase):
    """Both writers store the same records and deduplicate against each other."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='credvault-import-test-')
        self.addCleanup(shutil.rmtree, self.directory, True)

    def _write(self, name, lines):
        path = os.path.join(self.directory, name)
        with open(path, 'w', encoding='utf-8') as handle:
            handle.writelines(line + '\n' for line in lines)
        return path

    def test_profiles_are_defined_for_every_mode(self):
        self.assertEqual(set(IMPORT_PROFILES), set(QUERY_PROFILES))
        for profile in IMPORT_PROFILES.values():
            self.assertGreater(profile.batch_size, 0)
            self.assertGreaterEqual(profile.parse_workers, 1)

    def test_second_writer_creates_nothing_new(self):
        """
        Import one file with the Balanced writer, then the same file with the
        Fast writer. On PostgreSQL the second run goes through COPY and the SQL
        fingerprint, so this also checks that the two fingerprints agree.
        """
        path = os.path.join(self.directory, 'bulk.txt')
        write_synthetic_file(path, rows=400, seed=5)
        with open(path, 'r', encoding='utf-8') as handle:
            lines = [line.rstrip('\n') for line in handle]

        created_first, skipped_first = store_lines(
            lines, IMPORT_PROFILES['balanced'],
        )
        records_after_first = Account.objects.count()
        self.assertEqual(created_first, records_after_first)
        self.assertGreater(created_first, 0)

        created_second, skipped_second = store_lines(
            lines, IMPORT_PROFILES['fast'],
        )
        self.assertEqual(created_second, 0)
        self.assertEqual(skipped_second, skipped_first)
        self.assertEqual(Account.objects.count(), records_after_first)

    @skipUnless(is_postgresql(), 'COPY writer is PostgreSQL-only')
    def test_copy_writer_fingerprint_matches_python(self):
        from .fastimport import copy_import_rows

        row = ('https://fp.example.com/login', 'user', 'bob', 'secret', None)
        created, skipped = copy_import_rows(iter([row]))
        self.assertEqual((created, skipped), (1, 0))

        url = Url.objects.get(name='https://fp.example.com/login')
        user = User.objects.get(name='bob')
        password = Pass.objects.get(name='secret')
        fingerprint = record_fingerprint(
            url.id, 'user', None, user.id, password.id, None,
        )
        self.assertTrue(Account.objects.filter(fingerprint=fingerprint).exists())

        # The ORM writer must now find that record and create nothing.
        created, _skipped = store_lines(
            ['https://fp.example.com/login|bob|secret|'],
            IMPORT_PROFILES['balanced'],
        )
        self.assertEqual(created, 0)
        self.assertEqual(Account.objects.count(), 1)

    def test_import_events_continue_past_an_unreadable_source(self):
        good = self._write('good.txt', [
            'https://ok.example.com/login|bob|secret|',
        ])
        missing = Source(
            'path', os.path.join(self.directory, 'missing.txt'),
            'missing.txt', 0, None,
        )
        good_source = Source('path', good, 'good.txt', os.path.getsize(good), None)

        events = list(import_sources(
            [missing, good_source], IMPORT_PROFILES['balanced'],
        ))
        types = [event['type'] for event in events]
        self.assertIn('error', types)
        self.assertEqual(events[-1]['type'], 'done')
        self.assertEqual(events[-1]['created'], 1)
        errors = [event for event in events if event['type'] == 'error']
        self.assertEqual(errors[0]['reason'], 'unreadable')

    def test_done_event_reports_mode_and_rate(self):
        path = self._write('small.txt', [
            'https://rate.example.com/login|bob|secret|',
            'not a record at all',
        ])
        source = Source('path', path, 'small.txt', os.path.getsize(path), None)
        done = list(import_sources([source], IMPORT_PROFILES['balanced']))[-1]
        self.assertEqual(done['type'], 'done')
        self.assertEqual(done['lines'], 2)
        self.assertEqual(done['created'], 1)
        self.assertEqual(done['skipped'], 1)
        self.assertEqual(done['mode'], 'balanced')
        self.assertGreaterEqual(done['rate'], 0)


class CappedCountTests(TestCase):

    def setUp(self):
        self.url_objects = [
            Url.objects.create(name=f'https://cap{index}.example.com/login')
            for index in range(15)
        ]
        self.email = Email.objects.create(name='cap@example.com')
        self.password = Pass.objects.create(name='cap-secret')
        for url in self.url_objects:
            Account.objects.create(
                url=url, account_type='email', email=self.email,
                password=self.password,
            )

    def test_capped_count_stops_at_the_cap(self):
        self.assertEqual(capped_count(Account.objects.all(), 10), (10, True))

    def test_capped_count_is_exact_below_the_cap(self):
        self.assertEqual(capped_count(Account.objects.all(), 100), (15, False))

    def test_table_count_is_exact_unless_labelled_approximate(self):
        count, approximate = table_count(Account)
        if approximate:
            # PostgreSQL has statistics for this table from an earlier ANALYZE.
            self.assertGreaterEqual(count, 0)
        else:
            self.assertEqual(count, 15)

    @skipUnless(is_postgresql(), 'planner estimates are PostgreSQL-only')
    def test_postgresql_uses_the_planner_estimate_after_analyze(self):
        analyze_tables([Account])
        count, approximate = table_count(Account)
        self.assertTrue(approximate)
        self.assertEqual(count, 15)

    def test_paginator_shows_more_than_the_cap(self):
        paginator = CappedPaginator(Account.objects.order_by('-id'), 5, 10)
        self.assertEqual(paginator.count, 10)
        self.assertTrue(paginator.capped)

    def test_search_page_says_more_than_when_capped(self):
        client = _admin_client(self)
        balanced = QUERY_PROFILES['balanced']
        small_cap = replace(balanced, count_cap=5, page_size=50)
        with mock.patch.dict(QUERY_PROFILES, {'balanced': small_cap}):
            response = client.get(reverse('vault:advanced_search'), {
                'keyword': 'cap',
            })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'More than 5 matching records')

    @skipUnless(is_postgresql(), 'trigram index is PostgreSQL-only')
    def test_trigram_index_exists_and_prefix_index_matches_collation(self):
        names = index_names()
        self.assertIn('vault_url_name_trgm_idx', names)
        if text_collation_is_c('vault_url', 'name'):
            # The lower-name index serves prefix search under C; no duplicate.
            self.assertNotIn('vault_url_name_prefix_idx', names)
        else:
            self.assertIn('vault_url_name_prefix_idx', names)


class SearchApiTests(TestCase):

    def setUp(self):
        self.client = _admin_client(self)
        self.url = Url.objects.create(name='https://api.example.com/login')
        self.user = User.objects.create(name='api-user')
        self.password = Pass.objects.create(name='api-secret-value')
        self.ip = Ip.objects.create(name='198.51.100.9')
        Account.objects.create(
            url=self.url, account_type='user', user=self.user,
            password=self.password, ip=self.ip,
        )

    def test_api_returns_matches_without_passwords(self):
        response = self.client.get(reverse('vault:search_api'), {
            'field': 'url', 'q': 'https://api.example.com/login',
        })
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload['total'], 1)
        self.assertFalse(payload['total_capped'])
        self.assertEqual(payload['results'][0]['url'], 'https://api.example.com/login')
        self.assertEqual(payload['results'][0]['identifier'], 'api-user')
        self.assertNotIn('api-secret-value', response.content.decode())
        self.assertNotIn('password', payload['results'][0])

    def test_api_without_a_query_returns_nothing(self):
        response = self.client.get(reverse('vault:search_api'))
        payload = json.loads(response.content)
        self.assertEqual(payload, {
            'results': [], 'total': 0, 'total_capped': False,
            'query_ms': 0.0, 'query_mode': query_profile().key,
        })

    def test_advanced_search_with_bad_date_does_not_crash(self):
        response = self.client.get(reverse('vault:advanced_search'), {
            'created_from': 'not-a-date', 'keyword': 'api',
        })
        self.assertEqual(response.status_code, 200)

    def test_export_with_bad_date_returns_no_records(self):
        response = self.client.get(reverse('vault:export_csv'), {
            'adv': '1', 'created_from': '2026-13-99',
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('api-user', response.content.decode())

    def test_reference_suggest_lists_prefix_matches_first(self):
        Url.objects.create(name='https://zz-middle.example.com/')
        response = self.client.get(
            reverse('vault:reference_suggest', args=['url']), {'q': 'https://api'},
        )
        results = json.loads(response.content)['results']
        self.assertEqual(results[0], 'https://api.example.com/login')


class SettingsPageTests(TestCase):

    def setUp(self):
        self.client = _admin_client(self)

    def test_settings_page_is_linked_from_navigation(self):
        response = self.client.get(reverse('vault:dashboard'))
        self.assertContains(response, reverse('vault:settings'))

    def test_settings_page_saves_both_modes(self):
        response = self.client.post(reverse('vault:settings'), {
            'import_mode': 'fast', 'query_mode': 'maximum',
        })
        self.assertRedirects(response, reverse('vault:settings'))
        stored = PerformanceSettings.load()
        self.assertEqual((stored.import_mode, stored.query_mode), ('fast', 'maximum'))
        self.assertEqual(import_profile().key, 'fast')
        self.assertEqual(query_profile().key, 'maximum')

    def test_settings_page_rejects_an_unknown_mode(self):
        response = self.client.post(reverse('vault:settings'), {
            'import_mode': 'turbo', 'query_mode': 'balanced',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PerformanceSettings.load().import_mode, 'balanced')

    def test_settings_page_renders_index_status_on_postgresql_only(self):
        response = self.client.get(reverse('vault:settings'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="import_mode"')
        if is_postgresql():
            self.assertContains(response, 'Contains index (trigram)')
        else:
            self.assertNotContains(response, 'Contains index (trigram)')

    def test_import_page_names_the_active_mode(self):
        response = self.client.get(reverse('vault:import'))
        self.assertContains(response, 'Import speed:')

    def test_settings_page_shows_postgresql_version_when_used(self):
        response = self.client.get(reverse('vault:settings'))
        if is_postgresql():
            self.assertRegex(
                response.content.decode(), r'PostgreSQL \d+\.\d+',
            )


@skipUnless(connection.vendor == 'sqlite', 'SQLite exact-count fallback')
class SqliteCountTests(TestCase):

    def test_sqlite_counts_are_always_exact(self):
        Url.objects.create(name='https://sqlite-count.example.com/')
        count, approximate = table_count(Url)
        self.assertEqual((count, approximate), (1, False))
