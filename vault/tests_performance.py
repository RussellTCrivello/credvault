from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from .counts import CappedPaginator, capped_count, count_label
from .importer import bulk_import_lines
from .models import Account, Email, Pass, Url, User
from .utils import parse_line

PASSWORD = 'correct-horse-battery'


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


class CappedCountTests(TestCase):
    def setUp(self):
        # Records go through the loader, the real write path for bulk data.
        bulk_import_lines(
            parse_line(f'https://example.com/login|user{i}|pw{i}')
            for i in range(25)
        )

    def test_exact_below_the_cap(self):
        self.assertEqual(capped_count(Account.objects.all(), cap=100), (25, False))

    def test_stops_at_the_cap(self):
        self.assertEqual(capped_count(Account.objects.all(), cap=10), (10, True))

    def test_label_marks_capped_totals(self):
        self.assertEqual(count_label(10000, True), '10,000+')
        self.assertEqual(count_label(42, False), '42')

    def test_paginator_uses_the_cap_not_a_full_count(self):
        paginator = CappedPaginator(
            Account.objects.order_by('id'), per_page=10, cap=20,
        )
        self.assertEqual(paginator.count, 20)
        self.assertTrue(paginator.capped)
        self.assertEqual(paginator.num_pages, 2)

    @override_settings(VAULT_COUNT_CAP=5)
    def test_search_page_shows_the_plus_when_capped(self):
        get_user_model().objects.create_superuser('admin', 'a@example.com', PASSWORD)
        self.client.login(username='admin', password=PASSWORD)
        response = self.client.get(
            reverse('vault:search'), {'field': 'url', 'q': 'https://example.com/login'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '5+')


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
