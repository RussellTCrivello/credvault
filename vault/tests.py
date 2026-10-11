import json
import os
import shutil
import tempfile
import time

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from .importer import bulk_import_lines
from .models import Account, Email, Ip, Pass, Url, User
from .utils import parse_line


class WorkspaceInterfaceTests(TestCase):
    def setUp(self):
        self.url = Url.objects.create(name='https://example.com/login')
        self.email = Email.objects.create(name='person@example.com')
        self.password = Pass.objects.create(name='test-secret')
        self.ip = Ip.objects.create(name='203.0.113.10')
        self.record = Account.objects.create(
            url=self.url,
            account_type='email',
            email=self.email,
            password=self.password,
            ip=self.ip,
        )

    def test_workspace_pages_render(self):
        pages = (
            ('vault:dashboard', 'Your workspace'),
            ('vault:browse', 'Records'),
            ('vault:search', 'Search records'),
            ('vault:add', 'Add a record'),
            ('vault:import', 'Import records'),
            ('vault:advanced_search', 'Advanced search'),
            ('vault:references_index', 'Reference tables'),
        )

        for route_name, expected_text in pages:
            with self.subTest(page=route_name):
                response = self.client.get(reverse(route_name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, expected_text)
                self.assertContains(response, 'monitoring/admin.css')

    def test_record_detail_edit_and_delete_pages_render(self):
        pages = (
            ('vault:detail', 'Record details'),
            ('vault:update', 'Edit record'),
            ('vault:delete', 'Delete this record?'),
        )

        for route_name, expected_text in pages:
            with self.subTest(page=route_name):
                response = self.client.get(
                    reverse(route_name, args=[self.record.id])
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, expected_text)

    def test_no_records_displayed_until_queried(self):
        # Neither /records/ nor /search/ should dump records without a query.
        browse_res = self.client.get(reverse('vault:browse'))
        self.assertEqual(browse_res.status_code, 200)
        self.assertNotContains(browse_res, 'person@example.com')

        search_res = self.client.get(reverse('vault:search'))
        self.assertEqual(search_res.status_code, 200)
        self.assertNotContains(search_res, 'person@example.com')

        # Once queried, records are displayed immediately.
        queried_res = self.client.get(
            reverse('vault:browse'), {'q': 'example.com'}
        )
        self.assertContains(queried_res, 'person@example.com')

    def test_add_and_edit_record_fields_are_searchable_dropdown_comboboxes(self):
        User.objects.create(name='combo-user')
        for url in (
            reverse('vault:add'),
            reverse('vault:update', args=[self.record.id]),
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            for kind in ('url', 'email', 'user', 'pass', 'ip'):
                self.assertContains(response, f'data-combobox="{kind}"')
                self.assertContains(response, f'id="{kind}-list"')
            self.assertContains(response, 'data-combobox-select="true"')
            self.assertContains(response, 'class="combobox-dropdown"')
            self.assertContains(response, 'class="combobox-toggle"')

        # Live filter endpoint returns matching options for each reference table
        checks = (
            ('url', 'example.com', 'https://example.com/login'),
            ('email', 'person', 'person@example.com'),
            ('user', 'combo', 'combo-user'),
            ('pass', 'test-', 'test-secret'),
            ('ip', '203.0', '203.0.113.10'),
        )
        for kind, query, expected in checks:
            res = self.client.get(
                reverse('vault:reference_suggest', args=[kind]),
                {'q': query},
            )
            self.assertEqual(res.status_code, 200)
            self.assertIn(expected, res.json()['results'])

    def test_add_user_account_saves_and_redirects_to_details(self):
        response = self.client.post(
            reverse('vault:add'),
            {
                'account_type': 'user',
                'url_name': 'https://new.example/login',
                'user_name': 'new-user',
                'password_name': 'new-secret',
                'ip_name': 'N/A',
            },
        )

        self.assertRedirects(
            response,
            reverse('vault:detail', args=[self.record.id + 1]),
        )
        self.assertTrue(
            Account.objects.filter(
                url__name='https://new.example/login',
                user__name='new-user',
            ).exists()
        )

    def test_reference_values_are_reused_not_duplicated(self):
        # Adding two accounts that share a URL must reuse the same Url row.
        Account.objects.create(
            url=Url.objects.create(name='https://shared.example/login'),
            account_type='user',
            user=User.objects.create(name='alice'),
            password=Pass.objects.create(name='secret-a'),
        )
        Account.objects.create(
            url=Url.objects.get(name='https://shared.example/login'),
            account_type='user',
            user=User.objects.create(name='bob'),
            password=Pass.objects.create(name='secret-b'),
        )
        self.assertEqual(
            Url.objects.filter(name='https://shared.example/login').count(), 1
        )
        self.assertEqual(Account.objects.count(), 3)

    def test_duplicate_account_is_rejected(self):
        payload = {
            'account_type': 'email',
            'url_name': self.url.name,
            'email_name': self.email.name,
            'password_name': self.password.name,
            'ip_name': self.ip.name,
        }
        response = self.client.post(reverse('vault:add'), payload)
        # The duplicate guard blocks the second identical relationship.
        self.assertEqual(Account.objects.count(), 1)
        self.assertContains(response, 'already exists')

    def test_search_finds_by_password_without_listing_it(self):
        response = self.client.get(
            reverse('vault:search'),
            {'keyword': 'test-secret'},
        )

        self.assertContains(response, 'example.com')
        # The secret is never rendered as a result cell (only echoed in the
        # search box), so it must not appear inside a table cell.
        self.assertNotContains(response, 'test-secret</td>')

    def test_search_by_domain_email_url_and_password_is_subsecond(self):
        queries = [
            {'q': 'example.com'},
            {'field': 'domain', 'q': 'example.com'},
            {'field': 'url', 'q': 'example.com'},
            {'field': 'url', 'q': 'https://example.com/login'},
            {'q': 'person@example.com'},
            {'field': 'email', 'q': 'person@example.com'},
            {'q': 'test-secret'},
            {'field': 'password', 'q': 'test-secret'},
        ]
        for params in queries:
            with self.subTest(params=params):
                start = time.perf_counter()
                response = self.client.get(reverse('vault:search'), params)
                elapsed = time.perf_counter() - start
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'person@example.com')
                self.assertLess(elapsed, 1.0)

    def test_advanced_search_filters_each_field(self):
        response = self.client.get(
            reverse('vault:advanced_search'),
            {'account_type': 'email', 'email': 'person@example.com'},
        )
        self.assertContains(response, 'example.com')

        response = self.client.get(
            reverse('vault:advanced_search'),
            {'account_type': 'user', 'email': 'person@example.com'},
        )
        self.assertContains(response, 'No matching records')

    def test_import_page_accepts_supported_text_file(self):
        uploaded_file = SimpleUploadedFile(
            'records.txt',
            b'https://import.example/login|import-user|import-secret|203.0.113.20',
            content_type='text/plain',
        )

        response = self.client.post(
            reverse('vault:import'),
            {'file': uploaded_file},
        )

        self.assertRedirects(response, reverse('vault:browse'))
        self.assertTrue(
            Url.objects.filter(name='https://import.example/login').exists()
        )
        self.assertTrue(
            Account.objects.filter(user__name='import-user').exists()
        )

    def test_import_page_accepts_full_folder_upload_and_folder_path(self):
        # 1. Browser folder upload with multiple files and invalid lines
        f1 = SimpleUploadedFile(
            'f1.txt',
            b'# comment line\nhttps://folder.example/login:folder_u1:pw1:10.0.0.1\ninvalid line\n',
            content_type='text/plain',
        )
        f2 = SimpleUploadedFile(
            'f2.txt',
            b'https://folder.example/login|u2@folder.example|pw1|10.0.0.2\n',
            content_type='text/plain',
        )
        response = self.client.post(
            reverse('vault:import'),
            {'folder': [f1, f2]},
        )
        self.assertRedirects(response, reverse('vault:browse'))
        self.assertEqual(
            Account.objects.filter(url__name='https://folder.example/login').count(),
            2,
        )

        # 2. Local/server directory path with nested subdirectories
        tmp_dir = tempfile.mkdtemp()
        try:
            sub_dir = os.path.join(tmp_dir, 'nested')
            os.makedirs(sub_dir, exist_ok=True)
            with open(os.path.join(tmp_dir, 'root.txt'), 'w') as fh:
                fh.write('https://path.example/login;path_user1;secret1;10.1.1.1\n')
                fh.write('not a valid record line\n')
            with open(os.path.join(sub_dir, 'deep.log'), 'w') as fh:
                fh.write('https://path.example/login\tpath_user2@path.example\tsecret1\n')

            response = self.client.post(
                reverse('vault:import'),
                {'folder_path': tmp_dir},
            )
            self.assertRedirects(response, reverse('vault:browse'))
            self.assertEqual(
                Account.objects.filter(url__name='https://path.example/login').count(),
                2,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_importing_duplicate_line_reuses_values(self):
        line = (
            b'https://dup.example/login|dup-user|dup-secret|203.0.113.30'
        )
        Account.objects.create(
            url=Url.objects.create(name='https://dup.example/login'),
            account_type='user',
            user=User.objects.create(name='dup-user'),
            password=Pass.objects.create(name='dup-secret'),
            ip=Ip.objects.create(name='203.0.113.30'),
        )
        before_urls = Url.objects.count()
        before_accounts = Account.objects.count()

        uploaded_file = SimpleUploadedFile('records.txt', line,
                                          content_type='text/plain')
        self.client.post(reverse('vault:import'), {'file': uploaded_file})

        # No new Url and no new Account should be created; values are reused.
        self.assertEqual(Url.objects.count(), before_urls)
        self.assertEqual(Account.objects.count(), before_accounts)

    def test_delete_requires_post_confirmation(self):
        response = self.client.post(
            reverse('vault:delete', args=[self.record.id]),
        )

        self.assertRedirects(response, reverse('vault:browse'))
        self.assertFalse(Account.objects.filter(id=self.record.id).exists())

    def test_reference_delete_is_blocked_while_in_use(self):
        self.client.post(
            reverse('vault:reference_delete', args=['email', self.email.id]),
        )
        # Still in use by self.record, so it must not be deleted.
        self.assertEqual(Email.objects.filter(id=self.email.id).count(), 1)

    def test_reference_add_reuses_existing_value(self):
        self.client.post(
            reverse('vault:references', args=['email']),
            {'name': 'dup@example.com'},
        )
        self.client.post(
            reverse('vault:references', args=['email']),
            {'name': 'dup@example.com'},
        )
        # The same value is stored once and reused, never duplicated.
        self.assertEqual(
            Email.objects.filter(name='dup@example.com').count(), 1
        )

    def test_language_switch_persists_and_renders_arabic_rtl_interfaces(self):
        response = self.client.post(
            reverse('set_language'),
            {'language': 'ar', 'next': reverse('vault:dashboard')},
        )

        self.assertRedirects(response, reverse('vault:dashboard'))
        response = self.client.get(reverse('vault:dashboard'))
        self.assertContains(response, '<html lang="ar" dir="rtl">')
        self.assertContains(response, 'مساحة عملك')
        self.assertContains(response, 'اللغة')
        self.assertContains(response, 'استخدام المظهر الداكن')

        translated_pages = (
            ('vault:browse', (), 'كل السجلات'),
            ('vault:search', (), 'البحث في السجلات'),
            ('vault:add', (), 'إضافة سجل'),
            ('vault:import', (), 'استيراد السجلات'),
            ('vault:detail', (self.record.id,), 'تفاصيل السجل'),
            ('vault:update', (self.record.id,), 'تعديل السجل'),
            ('vault:delete', (self.record.id,), 'هل تريد حذف هذا السجل؟'),
        )
        for route_name, arguments, translated_heading in translated_pages:
            with self.subTest(page=route_name):
                response = self.client.get(
                    reverse(route_name, args=arguments)
                )
                self.assertContains(response, translated_heading)
                self.assertContains(response, '<html lang="ar" dir="rtl">')

        response = self.client.get(reverse('vault:add'))
        self.assertContains(response, 'الرابط')
        self.assertContains(response, 'اسم المستخدم')

        csv_response = self.client.get(reverse('vault:export_csv'))
        self.assertIn('الرابط'.encode(), csv_response.content)

        response = self.client.post(
            reverse('set_language'),
            {'language': 'en', 'next': reverse('vault:dashboard')},
        )
        self.assertRedirects(response, reverse('vault:dashboard'))
        self.assertContains(
            self.client.get(reverse('vault:dashboard')), 'Your workspace'
        )

    def test_bulk_import_lines_deduplicates_references(self):
        Account.objects.all().delete()
        Url.objects.all().delete()
        Email.objects.all().delete()
        User.objects.all().delete()
        Pass.objects.all().delete()
        Ip.objects.all().delete()

        parsed = [
            {'domain': 'x', 'url': 'https://a.com', 'account_type': 'email',
             'account_identifier': 'e@a.com', 'secret_data': 'pw',
             'ip_address': '1.1.1.1'},
            {'domain': 'x', 'url': 'https://a.com', 'account_type': 'email',
             'account_identifier': 'e@a.com', 'secret_data': 'pw',
             'ip_address': '1.1.1.1'},
            {'domain': 'y', 'url': 'https://b.com', 'account_type': 'user',
             'account_identifier': 'bob', 'secret_data': 'pw',
             'ip_address': 'N/A'},
            None,
        ]
        created, skipped = bulk_import_lines(iter(parsed))
        self.assertEqual(created, 2)
        self.assertEqual(skipped, 1)
        self.assertEqual(Account.objects.count(), 2)
        self.assertEqual(Url.objects.count(), 2)
        self.assertEqual(Pass.objects.filter(name='pw').count(), 1)
        self.assertEqual(Email.objects.filter(name='e@a.com').count(), 1)
        self.assertEqual(User.objects.filter(name='bob').count(), 1)
        self.assertEqual(Ip.objects.count(), 1)

    def test_quick_search_by_url_is_exact(self):
        response = self.client.get(
            reverse('vault:search'),
            {'field': 'url', 'q': 'https://example.com/login'},
        )
        self.assertContains(response, 'person@example.com')
        # A partial non-domain value must not match (the lookup is exact).
        response = self.client.get(
            reverse('vault:search'), {'field': 'url', 'q': 'example'}
        )
        self.assertContains(response, 'No matching records')

    def test_export_csv_respects_search_filter(self):
        response = self.client.get(
            reverse('vault:export_csv'),
            {'field': 'url', 'q': 'https://example.com/login'},
        )
        content = b''.join(response.streaming_content)
        self.assertIn(b'person@example.com', content)
        self.assertIn(b'203.0.113.10', content)

    def test_export_exports_all_matching_results_beyond_first_page(self):
        lines = [
            parse_line(
                f"https://scale.example/login|user{i}@scale.example|shared-pass|10.0.0.1"
            )
            for i in range(150)
        ]
        created, _skipped = bulk_import_lines(iter(lines))
        self.assertEqual(created, 150)

        # Search page shows page 1 (100 rows) out of 150 total matches
        search_res = self.client.get(
            reverse('vault:search'), {'q': 'scale.example'}
        )
        self.assertEqual(search_res.context['total'], 150)
        self.assertEqual(len(search_res.context['records']), 100)

        # Exporting JSON and CSV for the same query exports all 150 matches in full
        json_res = self.client.get(
            reverse('vault:export_json'), {'q': 'scale.example'}
        )
        exported_json = json.loads(json_res.content.decode('utf-8'))
        self.assertEqual(len(exported_json), 150)

        csv_res = self.client.get(
            reverse('vault:export_csv'), {'q': 'scale.example'}
        )
        csv_lines = [
            line for line in csv_res.content.decode('utf-8').splitlines()
            if line.strip()
        ]
        # 1 header row + 150 data rows
        self.assertEqual(len(csv_lines), 151)

    def test_importfolder_command_ingests_a_directory(self):
        Account.objects.all().delete()
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, 'one.txt'), 'w') as handle:
                handle.write(
                    'https://cmd.example/login|u1@cmd.example|cmdpass|10.0.0.9\n'
                )
            with open(os.path.join(tmp, 'two.txt'), 'w') as handle:
                handle.write(
                    'https://cmd.example/login|u2@cmd.example|cmdpass|10.0.0.9\n'
                )
            call_command('importfolder', tmp)
            self.assertEqual(Account.objects.count(), 2)
            self.assertEqual(
                Url.objects.filter(name='https://cmd.example/login').count(), 1
            )
            self.assertEqual(Pass.objects.filter(name='cmdpass').count(), 1)
            self.assertEqual(Ip.objects.filter(name='10.0.0.9').count(), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
