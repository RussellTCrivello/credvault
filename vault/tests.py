import io
import json
import os
import shutil
import tempfile
import time

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

import pathlib

from . import import_jobs
from .importer import (
    ImportCancelled,
    bulk_import_lines,
    import_path,
    import_stream,
)
from .models import Account, Email, Ip, Pass, Url, User
from .utils import parse_line
from .views import local_import_paths_allowed


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
            ('vault:references', 'Emails', 'email'),
        )

        for route_name, expected_text, *args in pages:
            with self.subTest(page=route_name):
                response = self.client.get(reverse(route_name, args=args))
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
            ('vault:advanced_search', (), 'بحث متقدم'),
            ('vault:references_index', (), 'الجداول المرجعية'),
        )

        response = self.client.get(reverse('vault:import'))
        self.assertContains(response, 'من هذا الجهاز')
        self.assertContains(response, 'من مسار على الخادم')
        response = self.client.get(reverse('vault:advanced_search'))
        self.assertContains(response, 'البحث في')
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

    # ------------------------------------------------------------------
    # Filtering interface

    # ------------------------------------------------------------------

    def test_references_page_filters_matches_and_sorts(self):
        referenced = Url.objects.create(name='https://alpha.example/login')
        Url.objects.create(name='https://other.example/path')
        base = reverse('vault:references', args=['url'])

        # The default mode is "starts with", which the index can serve.
        prefix = self.client.get(base, {'q': 'https://alpha'})
        self.assertEqual(
            [entry.name for entry in prefix.context['entities']],
            ['https://alpha.example/login'],
        )
        # A partial that only appears inside the value needs "contains".
        self.assertEqual(
            [entry.name for entry in
             self.client.get(base, {'q': 'alpha'}).context['entities']],
            [],
        )
        contains = self.client.get(base, {'q': 'example/path',
                                          'match': 'contains'})
        self.assertEqual([entry.name for entry in contains.context['entities']],
                         ['https://other.example/path'])
        exact = self.client.get(base, {'q': 'alpha', 'match': 'exact'})
        self.assertEqual(list(exact.context['entities']), [])
        exact_hit = self.client.get(
            base, {'q': 'https://alpha.example/login', 'match': 'exact'}
        )
        self.assertEqual(
            [entry.name for entry in exact_hit.context['entities']],
            ['https://alpha.example/login'],
        )

        # Usage counts are annotated, so a value in use is visible and its
        # deletion is refused rather than silently cascading.
        Account.objects.create(
            url=referenced,
            account_type='user',
            user=User.objects.create(name='someone'),
            password=Pass.objects.create(name='pw'),
        )
        self.assertContains(self.client.get(base), 'Used by')

        # Order is a control of its own: A-Z, Z-A, most used, newest.
        names = lambda response: [  # noqa: E731
            entry.name for entry in response.context['entities']
        ]
        self.assertEqual(names(self.client.get(base, {'sort': 'name'})), [
            'https://alpha.example/login', 'https://example.com/login',
            'https://other.example/path',
        ])
        self.assertEqual(names(self.client.get(base, {'sort': 'name_desc'})), [
            'https://other.example/path', 'https://example.com/login',
            'https://alpha.example/login',
        ])
        self.assertEqual(
            names(self.client.get(base, {'sort': 'used'}))[0],
            'https://alpha.example/login',
        )
        self.assertEqual(
            names(self.client.get(base, {'sort': 'recent'}))[0],
            'https://other.example/path',
        )
        # Nonsense parameters fall back to the default instead of erroring.
        bogus = self.client.get(base, {'sort': 'nope', 'match': 'nope'})
        self.assertEqual(bogus.status_code, 200)
        self.assertEqual(bogus.context['sort_value'], 'name')
        self.assertEqual(bogus.context['filter_mode'], 'prefix')

    def test_unused_reference_value_can_be_deleted(self):
        gone = Url.objects.create(name='https://unused.example/')
        response = self.client.post(
            reverse('vault:reference_delete', args=['url', gone.id]),
        )
        self.assertRedirects(
            response, reverse('vault:references', args=['url'])
        )
        self.assertFalse(Url.objects.filter(id=gone.id).exists())

    def test_active_filters_are_chips_that_clear_one_field_at_a_time(self):
        response = self.client.get(reverse('vault:search'), {
            'field': 'password',
            'q': 'test-secret',
            'account_type': 'email',
        })
        self.assertContains(response, 'active-filters')
        self.assertContains(response, 'filter-chips')
        self.assertContains(response, '3 active filters')
        # Each chip names its own field and links back without it, so one
        # click removes exactly one condition.
        self.assertContains(response, 'Remove the Value filter')
        self.assertContains(response, 'Clear all')
        chips = response.context['filters']['chips']
        self.assertEqual(
            sorted(chip['label'] for chip in chips),
            ['Account type', 'Search in', 'Value'],
        )
        # A chip link carries every other filter, so one click removes one
        # condition and the results stay on screen.
        value_chip = [c for c in chips if c['label'] == 'Value'][0]
        self.assertNotIn('q=', value_chip['clear'])
        self.assertIn('account_type=email', value_chip['clear'])
        self.assertIn('field=password', value_chip['clear'])

    def test_browse_lists_nothing_until_a_filter_is_given(self):
        response = self.client.get(reverse('vault:browse'), {'field': 'url'})
        self.assertContains(response, 'Nothing listed yet')
        self.assertNotContains(response, 'active-filters')
        # A value turns the same page into a result table.
        filled = self.client.get(reverse('vault:browse'),
                                 {'field': 'url', 'q': 'https://example.com/login'})
        self.assertContains(filled, 'Search results')
        self.assertNotContains(filled, 'Nothing listed yet')

    def test_advanced_search_groups_every_filter_field(self):
        response = self.client.get(reverse('vault:advanced_search'))
        for title in ('Search everywhere', 'Where the record points',
                      'Who owns it', 'The secret', 'When it was stored',
                      'Order the results'):
            self.assertContains(response, title)
        for name in ('keyword', 'url', 'ip', 'email', 'username', 'password',
                     'account_type', 'created_from', 'created_to', 'sort'):
            self.assertContains(response, 'name="%s"' % name)
        # Segmented controls are real radios, so they work without JavaScript.
        self.assertContains(response, 'segment-input')
        self.assertContains(response, 'checked')

    def test_filter_inputs_carry_their_state_back_into_the_form(self):
        response = self.client.get(reverse('vault:search'), {
            'field': 'email', 'q': 'person', 'account_type': 'user',
        })
        self.assertContains(response, 'value="person"')
        # The scope segment comes back checked, so the page reads the same way
        # it was searched.
        self.assertContains(response, 'id="field-email"')
        self.assertContains(response, 'checked')

    # ------------------------------------------------------------------
    # Import workspace

    # ------------------------------------------------------------------

    def test_import_page_offers_both_sources_and_hides_the_path_field(self):
        with self.settings(VAULT_ALLOW_LOCAL_IMPORT_PATHS=False):
            response = self.client.get(reverse('vault:import'))
        self.assertContains(response, 'From this device')
        self.assertContains(response, 'Drop files or a folder here')
        self.assertContains(response, 'Add a folder')
        # The server-path tab is still there, but disabled and not submittable.
        self.assertContains(response, 'disabled')
        self.assertNotContains(response, 'name="folder_path"')
        self.assertNotContains(response, 'Review files first')
        self.assertContains(response, 'monitoring/import.js')
        self.assertContains(response, 'import-urls')
        self.assertContains(response, 'import-labels')

        # Opted in, the dry run over a folder appears with the field.
        response = self.client.get(reverse('vault:import'))
        self.assertContains(response, 'name="folder_path"')
        self.assertContains(response, 'Review files first')

    def test_local_import_paths_need_an_explicit_opt_in(self):
        self.assertTrue(local_import_paths_allowed())
        with self.settings(VAULT_ALLOW_LOCAL_IMPORT_PATHS=False):
            self.assertFalse(local_import_paths_allowed())
            response = self.client.get(reverse('vault:import'))
        self.assertNotContains(response, 'name="folder_path"')
        # Opted in, the field is part of the form again.
        response = self.client.get(reverse('vault:import'))
        self.assertContains(response, 'name="folder_path"')

    def test_local_folder_import_is_refused_without_the_opt_in(self):
        before = Account.objects.count()
        with self.settings(VAULT_ALLOW_LOCAL_IMPORT_PATHS=False):
            response = self.client.post(
                reverse('vault:import'), {'folder_path': '/tmp'},
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, 'Importing from a server path is disabled'
        )
        self.assertEqual(Account.objects.count(), before)

    def test_ajax_import_answers_json_and_reports_progress(self):
        upload = SimpleUploadedFile(
            'batch.txt',
            b'https://ajax.example/login|ajax-user|ajax-pass|10.9.9.9\n'
            b'not a record\n',
            content_type='text/plain',
        )
        response = self.client.post(
            reverse('vault:import'),
            {'folder': [upload], 'relpath': ['nested/batch.txt']},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        summary = payload['summary']
        self.assertEqual(summary['created'], 1)
        self.assertEqual(summary['invalid'], 1)
        self.assertEqual(summary['lines'], 2)
        self.assertEqual(summary['percent'], 100.0)
        self.assertEqual(summary['state'], 'done')
        self.assertEqual(
            Account.objects.filter(user__name='ajax-user').count(), 1
        )

        # The per-file rows come from the status endpoint: the same numbers
        # the browser polls while the import is still in flight. The browser's
        # own path is kept so "nested/batch.txt" is what the user reads.
        status = self.client.get(
            reverse('vault:import_job_status', args=[payload['job_id']])
        )
        self.assertEqual(status.status_code, 200)
        progress = status.json()['progress']
        self.assertTrue(progress['finished'])
        row = progress['files'][0]
        self.assertEqual(row['path'], 'nested/batch.txt')
        self.assertEqual(row['created'], 1)
        self.assertEqual(row['invalid'], 1)
        self.assertEqual(row['state'], 'done')

    def test_binary_files_are_skipped_with_a_warning(self):
        uploads = [
            SimpleUploadedFile('good.txt', b'https://ok/x|u|p\n',
                               content_type='text/plain'),
            SimpleUploadedFile('picture.png', b'\x89PNG\r\n\x1a\n' + b'x' * 40,
                               content_type='image/png'),
        ]
        response = self.client.post(
            reverse('vault:import'),
            {'folder': uploads},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        payload = response.json()
        self.assertEqual(payload['summary']['created'], 1)
        self.assertEqual(payload['summary']['files_total'], 1)
        self.assertIn('picture.png', payload['summary']['warnings'][0])

    def test_import_prepare_reports_the_folder_without_reading_it(self):
        before = Account.objects.count()
        with tempfile.TemporaryDirectory() as tmp:
            text = 'https://a/x|u|p\n'
            pathlib.Path(tmp, 'one.txt').write_text(text)
            pathlib.Path(tmp, 'ignored.png').write_bytes(b'\x89PNG' + b'0' * 20)
            with self.settings(VAULT_ALLOW_LOCAL_IMPORT_PATHS=True):
                response = self.client.post(
                    reverse('vault:import_prepare'),
                    data=json.dumps({'mode': 'path', 'folder_path': tmp}),
                    content_type='application/json',
                    HTTP_X_REQUESTED_WITH='XMLHttpRequest',
                )
            payload = response.json()
            self.assertTrue(payload['ok'])
            self.assertEqual(payload['files'], 1)
            self.assertEqual(payload['skipped'], 1)
            self.assertEqual(payload['bytes'], len(text))
            self.assertEqual(payload['progress']['state'], 'waiting')
            self.assertEqual(payload['progress']['bytes_total'], len(text))
            # Preparing never reads the file: there is nothing imported yet.
            self.assertEqual(payload['progress']['created'], 0)
            self.assertEqual(Account.objects.count(), before)

            status = self.client.get(
                reverse('vault:import_job_status', args=[payload['job_id']])
            )
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json()['progress']['percent'], 0.0)
            # Preparing is a dry run: the folder is listed, not read.
            self.assertEqual(Account.objects.count(), before)

    def test_unknown_import_job_is_reported_as_json_404(self):
        response = self.client.get(
            reverse('vault:import_job_status', args=['does-not-exist'])
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['error'], 'unknown_job')

    def test_stop_endpoint_marks_the_job_and_returns_its_progress(self):
        upload = SimpleUploadedFile('one.txt', b'https://s/x|u|p\n',
                                    content_type='text/plain')
        started = self.client.post(
            reverse('vault:import'),
            {'folder': [upload]},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        ).json()
        response = self.client.post(
            reverse('vault:import_job_stop', args=[started['job_id']]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        # The job is already finished, so stopping it is a no-op that still
        # answers with the last known progress.
        self.assertEqual(response.json()['progress']['created'], 1)

    def test_html_import_still_works_without_javascript(self):
        upload = SimpleUploadedFile(
            'plain.txt',
            b'https://nojs.example/login|nojs|pass|10.1.1.1\n',
            content_type='text/plain',
        )
        response = self.client.post(
            reverse('vault:import'), {'file': upload}, follow=True
        )
        self.assertRedirects(response, reverse('vault:browse'))
        self.assertEqual(Account.objects.filter(user__name='nojs').count(), 1)
        messages = [str(message) for message in response.context['messages']]
        self.assertTrue(
            any('Successfully imported 1 record' in message
                for message in messages),
            messages,
        )

    # ------------------------------------------------------------------
    # Import engine and progress tracking

    # ------------------------------------------------------------------

    def test_stats_breakdown_accounts_for_every_line(self):
        payload = [
            'https://site%d/login|user%d|pw%d|10.0.0.1' % (i, i, i)
            for i in range(1200)
        ]
        payload += ['', '# comment', 'garbage', payload[0]]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp, 'all.txt')
            path.write_text('\n'.join(payload) + '\n')
            with path.open('rb') as handle:
                stats = import_stream(handle)
            with path.open('rb') as handle:
                again = import_stream(handle)

        created, skipped = stats
        self.assertEqual(created, 1200)
        self.assertEqual(skipped, 3)
        # Every line is classified exactly once, and every valid line is
        # either stored or recognised as a repeat of an earlier one.
        self.assertEqual(stats.lines, len(payload))
        self.assertEqual(stats.lines, stats.blank + stats.invalid + stats.valid)
        self.assertEqual(stats.valid, stats.created + stats.duplicates)
        self.assertEqual(stats.blank, 2)
        self.assertEqual(stats.invalid, 1)
        self.assertEqual(stats.duplicates, 1)
        self.assertEqual(again.created, 0)
        self.assertEqual(again.duplicates, 1201)

    def test_progress_is_reported_per_chunk_and_per_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = '\n'.join(
                'https://a%d/x|u%d|p%d' % (i, i, i) for i in range(1200)
            ) + '\n'
            pathlib.Path(tmp, 'a.txt').write_text(big)
            small = 'https://b/x|u|p\n'
            pathlib.Path(tmp, 'b.txt').write_text(small)

            seen = {'bytes': 0, 'begun': [], 'ended': [], 'notes': 0}

            class Tracker:
                def file_bytes(self, count):
                    seen['bytes'] += count

                def note(self, **kwargs):
                    seen['notes'] += 1

                def file_error(self, message):
                    raise AssertionError(message)

                def stopped(self):
                    return False

            def on_start(filepath, index, files_total):
                seen['begun'].append((os.path.basename(filepath), index,
                                      files_total))

            def on_file(filepath, stats, total_created, index, files_total):
                seen['ended'].append((os.path.basename(filepath),
                                      stats.created, total_created))

            created, skipped, count = import_path(
                tmp, tracker=Tracker(), on_start=on_start, on_file=on_file
            )

        self.assertEqual(count, 2)
        self.assertEqual(created, 1201)
        self.assertEqual(
            seen['begun'], [('a.txt', 1, 2), ('b.txt', 2, 2)]
        )
        self.assertEqual(
            seen['ended'], [('a.txt', 1200, 1200), ('b.txt', 1, 1201)]
        )
        # Byte accounting is exact, which is what makes the percentage honest.
        self.assertEqual(seen['bytes'], len(big) + len(small))
        # Progress is pushed while reading, not only when a file ends.
        self.assertGreater(seen['notes'], 2)

    def test_cancelting_keeps_every_line_already_read(self):
        class EveryFourth:
            def __init__(self):
                self.calls = 0

            def file_bytes(self, count):
                pass

            def note(self, **kwargs):
                pass

            def file_error(self, message):
                pass

            def stopped(self):
                self.calls += 1
                return self.calls > 3

        parsed = (
            {'url': 'https://z%d/' % i, 'secret_data': 'p',
             'account_identifier': 'u%d' % i, 'account_type': 'user'}
            for i in range(20000)
        )
        before = Account.objects.count()
        with self.assertRaises(ImportCancelled):
            bulk_import_lines(parsed, tracker=EveryFourth())
        # Stopping is not a rollback: the batches already written stay.
        self.assertGreater(Account.objects.count(), before)

    def test_import_job_percent_is_byte_based_and_monotonic(self):
        job = import_jobs.create(label='progress', source_kind='path',
                                 register_files=False)
        first = job.snapshot(include_files=False)
        self.assertEqual(first['state'], 'waiting')
        self.assertEqual(first['stage'], 'uploading')
        # No size is known yet, so no percentage is claimed: the UI shows an
        # indeterminate bar instead of a fake 0%.
        self.assertIsNone(first['percent'])
        self.assertEqual(first['bytes_total'], 0)
        self.assertEqual(first['files'], [])

        job.bytes_total = 1000
        job.mark_scanning()
        scanning = job.snapshot(include_files=False)
        self.assertEqual(scanning['stage'], 'scanning')
        self.assertEqual(scanning['percent'], 0.0)
        self.assertEqual(scanning['stage_index'], 1)
        self.assertEqual(len(scanning['stages']), 4)

        job.file_bytes(250)
        quarter = job.snapshot(include_files=False)
        self.assertEqual(quarter['percent'], 25.0)
        self.assertEqual(quarter['bytes_remaining'], 750)

        job.note(created=10, lines=40)
        more = job.snapshot(include_files=False)
        self.assertEqual(more['percent'], 25.0)
        self.assertEqual(more['created'], 10)
        self.assertEqual(more['lines'], 40)

        # Nothing moves backwards, even if a source reports fewer bytes.
        job.bytes_done = 100
        self.assertGreaterEqual(
            job.snapshot(include_files=False)['percent'], 25.0
        )

        job.finish()
        done = job.snapshot(include_files=False)
        self.assertEqual(done['percent'], 100.0)
        self.assertTrue(done['finished'])
        self.assertEqual(done['stage'], 'completed')
        self.assertEqual(done['state'], 'done')

    def test_import_job_survives_a_progress_reader_that_arrives_late(self):
        job = import_jobs.create(label='late', source_kind='upload',
                                 register_files=False)
        job.warn('first')
        for index in range(import_jobs.MAX_WARNINGS + 5):
            job.warn('warning %d' % index)
        snapshot = job.snapshot(include_files=True)
        self.assertLessEqual(len(snapshot['warnings']), import_jobs.MAX_WARNINGS)
        self.assertGreater(snapshot['warnings_dropped'], 0)

    def test_job_registry_garbage_collects_finished_jobs(self):
        created = []
        for index in range(import_jobs.MAX_FINISHED_JOBS + 5):
            created.append(import_jobs.create(label='gc-%d' % index,
                                              source_kind='upload',
                                              register_files=False))
        for job in created:
            job.finish()
        import_jobs._prune_locked()
        kept = sum(1 for job in created if import_jobs.get(job.id) is job)
        self.assertLessEqual(kept, import_jobs.MAX_FINISHED_JOBS)

    def test_translation_catalogue_covers_every_string_the_app_uses(self):
        """
        The workspace ships a compiled Arabic catalogue, so a string added to
        a template without a translation is a bug the tests can actually catch:
        the audit command reads the sources, compares them with the catalogue
        and ``--strict`` turns any gap into a failure.
        """
        report = io.StringIO()
        call_command('translationaudit', '--strict', stdout=report)
        self.assertIn('0 missing', report.getvalue())
