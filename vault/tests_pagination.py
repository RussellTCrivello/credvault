"""Cursor (keyset) pagination: contract, stability and unchanged defaults."""

import re

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import Account, Email, Ip, Pass, Url


class KeysetPaginationTests(TestCase):
    TOTAL = 230  # more than two pages of 100

    def setUp(self):
        admin = get_user_model().objects.create_user(
            username='cursor-admin', password='x', is_staff=True)
        self.client.force_login(admin)
        self.url = Url.objects.create(name='https://cursor.example.com/login')
        self.ip = Ip.objects.create(name='203.0.113.7')
        self.ids = []
        for index in range(self.TOTAL):
            account = Account.objects.create(
                url=self.url, account_type='email',
                email=Email.objects.create(name=f'u{index}@cursor.example'),
                password=Pass.objects.create(name=f'pw-{index}'), ip=self.ip)
            self.ids.append(account.pk)

    def _walk(self, sort, before_second_page=None):
        params = {'q': 'cursor.example.com', 'field': 'domain',
                  'sort': sort, 'after': 0}
        seen, pages = [], 0
        while True:
            response = self.client.get(reverse('vault:search'), params)
            self.assertEqual(response.status_code, 200)
            page = response.context['page_obj']
            self.assertIsNone(page.paginator)
            self.assertIsNone(response.context['total_label'])
            seen.extend(record.pk for record in page.object_list)
            pages += 1
            if pages == 1 and before_second_page:
                before_second_page()
            if page.next_cursor is None:
                return seen, pages
            params['after'] = page.next_cursor

    def test_ascending_walk_visits_every_row_once_in_order(self):
        seen, pages = self._walk('id')
        self.assertEqual(seen, sorted(self.ids))
        self.assertEqual(pages, 3)

    def test_descending_walk_visits_every_row_once_in_order(self):
        seen, pages = self._walk('-id')
        self.assertEqual(seen, sorted(self.ids, reverse=True))
        self.assertEqual(pages, 3)

    def test_walk_is_stable_under_concurrent_inserts(self):
        inserted = []
        counter = iter(range(1000))

        def insert_rows():
            for _ in range(5):
                index = next(counter)
                inserted.append(Account.objects.create(
                    url=self.url, account_type='email',
                    email=Email.objects.create(name=f'late{index}@cursor.example'),
                    password=Pass.objects.create(name=f'late-pw-{index}'),
                    ip=self.ip).pk)

        seen, _ = self._walk('id', before_second_page=insert_rows)
        self.assertEqual(len(seen), len(set(seen)), 'no row repeated')
        self.assertEqual(seen, sorted(self.ids + inserted),
                         'rows added behind the cursor are reached, none skipped')

        # Descending: rows inserted after the walk started are ahead of the
        # cursor and are not shown; nothing is repeated or skipped.
        before = Account.objects.count()
        inserted.clear()
        seen, _ = self._walk('-id', before_second_page=insert_rows)
        self.assertEqual(len(seen), before)
        self.assertEqual(len(seen), len(set(seen)))

    def test_default_mode_keeps_exact_count_and_page_numbers(self):
        response = self.client.get(reverse('vault:search'), {
            'q': 'cursor.example.com', 'field': 'domain'})
        self.assertEqual(response.context['total'], self.TOTAL)
        self.assertContains(response, f'{self.TOTAL:,} matching records')
        self.assertContains(response, 'Page 1 of 3')
        self.assertNotContains(response, 'Cursor mode')

    def test_cursor_mode_requires_id_sort_and_valid_cursor(self):
        for params in ({'after': 5}, {'sort': 'id', 'after': 'x'},
                       {'sort': 'id', 'after': -1},
                       {'sort': 'url', 'after': 0}):
            with self.subTest(params=params):
                response = self.client.get(reverse('vault:search'), {
                    'q': 'cursor.example.com', 'field': 'domain', **params})
                self.assertEqual(response.context['total'], self.TOTAL)

    def test_cursor_page_does_not_issue_a_count_query(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse('vault:search'), {
                'q': 'cursor.example.com', 'field': 'domain',
                'sort': 'id', 'after': 0})
        counts = [q['sql'] for q in queries.captured_queries
                  if re.search(r'select count\(', q['sql'], re.I)
                  and 'vault_account' in q['sql']]
        # The records-total banner is not shown on the search page, so no
        # COUNT over vault_account is expected in cursor mode.
        self.assertEqual(counts, [])
