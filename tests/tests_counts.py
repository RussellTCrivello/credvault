"""Search-total cap and cached whole-table totals (vault.counts)."""

import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from vault import counts
from vault.models import Account, Email, Ip, Pass, Url


def _make_accounts(total, host='counts.example.com'):
    url = Url.objects.create(name=f'https://{host}/login')
    ip = Ip.objects.create(name='203.0.113.9')
    for index in range(total):
        Account.objects.create(
            url=url, account_type='email',
            email=Email.objects.create(name=f'c{index}@{host}'),
            password=Pass.objects.create(name=f'cpw-{index}'), ip=ip)


class BoundedCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _make_accounts(12)

    def test_exact_below_cap(self):
        self.assertEqual(
            counts.bounded_count(Account.objects.all(), 20), (12, False))

    def test_exactly_at_cap_is_not_reported_as_capped(self):
        self.assertEqual(
            counts.bounded_count(Account.objects.all(), 12), (12, False))

    def test_above_cap_reports_cap_and_flag(self):
        self.assertEqual(
            counts.bounded_count(Account.objects.all(), 5), (5, True))

    def test_bounded_query_reads_at_most_cap_plus_one_rows(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as queries:
            counts.bounded_count(Account.objects.all(), 5)
        self.assertIn('LIMIT 6', queries[0]['sql'])

    def test_label(self):
        self.assertEqual(counts.count_label(1234), '1,234')
        self.assertEqual(counts.count_label(1000, capped=True), '1,000+')

    def test_cap_environment_parsing(self):
        for raw, expected in (('', 0), ('0', 0), ('abc', 0), ('-5', 0),
                              ('250000', 250000),
                              ('99999999999999', counts.MAX_RESULT_COUNT_CAP)):
            with mock.patch.dict(os.environ, {'VAULT_RESULT_COUNT_CAP': raw}):
                self.assertEqual(counts.result_count_cap(), expected, raw)


class SearchTotalCapViewTests(TestCase):
    TOTAL = 230

    def setUp(self):
        admin = get_user_model().objects.create_user(
            username='cap-admin', password='x', is_staff=True)
        self.client.force_login(admin)
        _make_accounts(self.TOTAL)
        self.params = {'q': 'counts.example.com', 'field': 'domain'}

    def _get(self, **extra):
        return self.client.get(reverse('vault:search'), {**self.params, **extra})

    def test_default_is_exact_and_uncapped(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('VAULT_RESULT_COUNT_CAP', None)
            response = self._get()
        self.assertEqual(response.context['total'], self.TOTAL)
        self.assertEqual(response.context['total_label'], '230')
        self.assertEqual(response.context['page_obj'].paginator.num_pages, 3)

    def test_cap_labels_and_limits_pages_but_keeps_page_one_complete(self):
        with mock.patch.dict(os.environ, {'VAULT_RESULT_COUNT_CAP': '150'}):
            response = self._get()
            self.assertEqual(response.context['total'], 150)
            self.assertEqual(response.context['total_label'], '150+')
            self.assertEqual(len(response.context['records']), 100)
            self.assertContains(response, 'Totals are capped at 150')
            self.assertContains(response, 'Page 1 of 2+')
            second = self._get(page=2)
            self.assertEqual(len(second.context['records']), 50)

    def test_cap_above_matches_is_exact(self):
        with mock.patch.dict(os.environ, {'VAULT_RESULT_COUNT_CAP': '1000'}):
            response = self._get()
        self.assertEqual(response.context['total_label'], '230')
        self.assertNotContains(response, 'Totals are capped')

    def test_cursor_mode_still_walks_every_match_when_capped(self):
        with mock.patch.dict(os.environ, {'VAULT_RESULT_COUNT_CAP': '10'}):
            params = {'sort': 'id', 'after': 0}
            seen = 0
            while True:
                response = self._get(**params)
                page = response.context['page_obj']
                seen += len(page.object_list)
                if page.next_cursor is None:
                    break
                params['after'] = page.next_cursor
        self.assertEqual(seen, self.TOTAL)

    def test_export_is_not_capped(self):
        with mock.patch.dict(os.environ, {'VAULT_RESULT_COUNT_CAP': '10'}):
            response = self.client.get(
                reverse('vault:export_csv'), self.params)
            body = b''.join(response.streaming_content) if getattr(
                response, 'streaming', False) else response.content
        lines = [line for line in body.decode().splitlines() if line.strip()]
        self.assertEqual(len(lines), self.TOTAL + 1)  # header + every match


class TableTotalTests(TestCase):
    def setUp(self):
        cache.clear()
        _make_accounts(3)

    def tearDown(self):
        cache.clear()

    def _large(self, estimate=5_000_000):
        return mock.patch.object(counts, 'estimated_rows', return_value=estimate)

    def test_small_or_unanalyzed_tables_are_counted_live(self):
        with self._large(10):
            self.assertEqual(counts.table_total(Account), 3)
            Account.objects.filter(pk=Account.objects.first().pk).delete()
            self.assertEqual(counts.table_total(Account), 2)
        with self._large(None):
            self.assertEqual(counts.table_total(Account), 2)

    def test_large_table_total_is_cached_until_ttl_or_invalidation(self):
        with self._large():
            self.assertEqual(counts.table_total(Account), 3)
            Account.objects.filter(pk=Account.objects.first().pk).delete()
            self.assertEqual(counts.table_total(Account), 3)  # cached
            counts.invalidate_table_totals()
            self.assertEqual(counts.table_total(Account), 2)

    def test_ttl_expiry_recounts(self):
        with self._large(), mock.patch.object(counts, 'time') as clock_mod:
            clock_mod.time.return_value = 1000.0
            self.assertEqual(counts.table_total(Account), 3)
            Account.objects.filter(pk=Account.objects.first().pk).delete()
            clock_mod.time.return_value = 1000.0 + counts.total_count_ttl() - 1
            self.assertEqual(counts.table_total(Account), 3)
            clock_mod.time.return_value = 1000.0 + counts.total_count_ttl() + 1
            self.assertEqual(counts.table_total(Account), 2)

    def test_ttl_zero_always_live(self):
        with self._large(), mock.patch.dict(
                os.environ, {'VAULT_TOTAL_COUNT_TTL': '0'}):
            self.assertEqual(counts.table_total(Account), 3)
            Account.objects.filter(pk=Account.objects.first().pk).delete()
            self.assertEqual(counts.table_total(Account), 2)

    def test_concurrent_recount_serves_previous_value(self):
        with self._large(), mock.patch.object(counts, 'time') as clock_mod:
            clock_mod.time.return_value = 1000.0
            self.assertEqual(counts.table_total(Account), 3)
            Account.objects.filter(pk=Account.objects.first().pk).delete()
            clock_mod.time.return_value = 5000.0
            cache.add(counts._LOCK_KEY % Account._meta.db_table, 1, 60)
            self.assertEqual(counts.table_total(Account), 3)  # another worker recounting

    def test_estimate_comes_from_the_planner_statistics(self):
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute('ANALYZE vault_account')
        self.assertEqual(counts.estimated_rows(Account), 3)

    def test_dashboard_and_references_use_cached_totals(self):
        admin = get_user_model().objects.create_user(
            username='tot-admin', password='x', is_staff=True)
        self.client.force_login(admin)
        with self._large():
            first = self.client.get(reverse('vault:dashboard'))
            self.assertEqual(first.context['total_records'], '3')
            Account.objects.filter(pk=Account.objects.first().pk).delete()
            second = self.client.get(reverse('vault:dashboard'))
            self.assertEqual(second.context['total_records'], '3')
            self.client.post(
                reverse('vault:delete', args=[Account.objects.first().pk]))
            third = self.client.get(reverse('vault:dashboard'))
            self.assertEqual(third.context['total_records'], '1')

    def test_reference_list_total_uses_cached_paginator(self):
        admin = get_user_model().objects.create_user(
            username='ref-admin', password='x', is_staff=True)
        self.client.force_login(admin)
        response = self.client.get(reverse('vault:references', args=['email']))
        self.assertEqual(response.context['total_values'], 3)
