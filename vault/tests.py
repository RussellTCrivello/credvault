from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from .importer import bulk_import_lines, bulk_import_rows
from .models import Account, Email, Ip, Pass, Url, User


def stream_body(response):
    """A StreamingHttpResponse has to be collected before it can be read."""
    return b''.join(response.streaming_content)


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
            ('vault:browse', 'All records'),
            ('vault:search', 'Search records'),
            ('vault:advanced_search', 'Advanced search'),
            ('vault:add', 'Add a record'),
            ('vault:import', 'Import records'),
            ('vault:references_index', 'Reference tables'),
        )

        for route_name, expected_text in pages:
            with self.subTest(page=route_name):
                response = self.client.get(reverse(route_name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, expected_text)
                self.assertContains(response, 'monitoring/admin.css')

    def test_reference_pages_render_for_every_kind(self):
        for kind in ('url', 'email', 'user', 'pass', 'ip'):
            with self.subTest(kind=kind):
                response = self.client.get(
                    reverse('vault:references', args=[kind])
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Add a value')

        response = self.client.get(reverse('vault:references', args=['nope']))
        self.assertRedirects(response, reverse('vault:dashboard'))

    def test_record_detail_edit_and_delete_pages_render(self):
        pages = (
            ('vault:detail', 'Record details'),
            ('vault:update', 'Edit record'),
            ('vault:delete', 'Delete this record?'),
        )

        for route_name, expected_text in pages:
            with self.subTest(page=route_name):
                response = self.client.get(
                    reverse(route_name, args=[self.record.pk])
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, expected_text)

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
            reverse('vault:detail', args=[self.record.pk + 1]),
        )
        self.assertTrue(
            Account.objects.filter(
                url__name='https://new.example/login',
                user__name='new-user',
            ).exists()
        )
        # 'N/A' is a placeholder, never a stored IP value.
        self.assertFalse(Ip.objects.filter(name='N/A').exists())

    def test_reference_values_are_reused_not_duplicated(self):
        # Adding two accounts that share a URL must reuse the same Url row.
        shared = Url.objects.create(name='https://shared.example/login')
        Account.objects.create(
            url=shared, account_type='user',
            user=User.objects.create(name='alice'),
            password=Pass.objects.create(name='secret-a'),
        )
        Account.objects.create(
            url=shared, account_type='user',
            user=User.objects.create(name='bob'),
            password=Pass.objects.create(name='secret-b'),
        )
        self.assertEqual(
            Url.objects.filter(name='https://shared.example/login').count(), 1
        )
        self.assertEqual(Account.objects.count(), 3)

    def test_adding_an_existing_value_reuses_the_same_rows(self):
        self.client.post(
            reverse('vault:add'),
            {
                'account_type': 'email',
                'url_name': self.url.name,
                'email_name': self.email.name,
                'password_name': 'a-different-secret',
                'ip_name': self.ip.name,
            },
        )
        # Every component already existed, so only the relationship is new.
        self.assertEqual(Url.objects.count(), 1)
        self.assertEqual(Email.objects.count(), 1)
        self.assertEqual(Ip.objects.count(), 1)
        self.assertEqual(Pass.objects.count(), 2)
        self.assertEqual(Account.objects.count(), 2)

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

    def test_email_accounts_cannot_carry_a_username(self):
        response = self.client.post(
            reverse('vault:add'),
            {
                'account_type': 'email',
                'url_name': 'https://x.example/login',
                'email_name': 'someone@x.example',
                'user_name': 'someone',
                'password_name': 'pw',
            },
        )
        self.assertContains(response, 'cannot have a username')
        self.assertEqual(Account.objects.count(), 1)

    def test_quick_search_matches_exactly_on_every_field(self):
        cases = (
            ('domain', 'example.com', self.record.pk),
            ('url', self.url.name, self.record.pk),
            ('email', self.email.name, self.record.pk),
            ('password', self.password.name, self.record.pk),
            ('ip', self.ip.name, self.record.pk),
        )
        for field, value, expected_pk in cases:
            with self.subTest(field=field):
                response = self.client.get(
                    reverse('vault:search'), {'field': field, 'q': value}
                )
                self.assertContains(response, 'person@example.com')
                self.assertContains(response, '1 matching record')
                self.assertContains(
                    response, reverse('vault:detail', args=[expected_pk])
                )

    def test_quick_search_is_case_insensitive_but_not_partial(self):
        response = self.client.get(
            reverse('vault:search'),
            {'field': 'email', 'q': 'PERSON@example.com'},
        )
        self.assertContains(response, '1 matching record')

        response = self.client.get(
            reverse('vault:search'), {'field': 'url', 'q': 'example'}
        )
        self.assertContains(response, 'No matching records')

    def test_search_does_not_list_the_secret_as_a_result_cell(self):
        response = self.client.get(
            reverse('vault:search'), {'keyword': 'test-secret'}
        )
        self.assertContains(response, 'example.com')
        # The secret is never rendered as a result cell (only echoed in the
        # search box), so it must not appear inside a table cell.
        self.assertNotContains(response, 'test-secret</td>')

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

    def test_browse_is_paginated_but_counts_everything(self):
        bulk_import_lines(
            [f'https://many.example|user{i}|pw{i}|N/A' for i in range(150)]
        )

        response = self.client.get(reverse('vault:browse'))
        self.assertEqual(len(response.context['records']), 100)
        self.assertEqual(response.context['total'], 151)
        self.assertContains(response, 'Page 1 of 2')

    def test_export_returns_every_match_not_only_the_page(self):
        """The presentation is paginated; the export is the whole answer."""
        url = Url.objects.get_or_create(
            name='https://bulk.example/login', defaults={'host': 'bulk.example'}
        )[0]
        bulk_import_lines(
            [f'https://bulk.example/login|user{i}|pw{i}|N/A'
             for i in range(250)]
        )

        params = {'field': 'url', 'q': url.name}
        page = self.client.get(reverse('vault:search'), params)
        self.assertEqual(len(page.context['records']), 100)
        self.assertEqual(page.context['total'], 250)

        csv_response = self.client.get(reverse('vault:export_csv'), params)
        rows = stream_body(csv_response).decode('utf-8-sig').strip()\
            .splitlines()
        self.assertEqual(len(rows), 251)  # header + every matching record

        json_response = self.client.get(reverse('vault:export_json'), params)
        import json as json_module
        payload = json_module.loads(stream_body(json_response).decode())
        self.assertEqual(len(payload), 250)
        self.assertEqual(payload[0]['url'], url.name)

    def test_export_without_filters_covers_the_workspace(self):
        response = self.client.get(reverse('vault:export_csv'))
        rows = stream_body(response).decode('utf-8-sig').strip().splitlines()
        self.assertEqual(len(rows), 2)  # header + the single record
        self.assertIn('example.com', rows[1])
        self.assertIn('203.0.113.10', rows[1])


class ImportTests(TestCase):
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
        self.assertEqual(
            Url.objects.get(name='https://import.example/login').host,
            'import.example',
        )

    def test_import_accepts_several_files_in_one_folder_pick(self):
        first = SimpleUploadedFile(
            'a.txt',
            b'https://a.example|one@aa.example|p1|10.0.0.1\n'
            b'# just a comment\n'
            b'not a record\n',
            content_type='text/plain',
        )
        second = SimpleUploadedFile(
            'b.txt',
            b'https://b.example\ttwo@bb.example\tp2\t10.0.0.2\n',
            content_type='text/plain',
        )

        response = self.client.post(
            reverse('vault:import'),
            {'folder': [first, second]},
        )
        self.assertRedirects(response, reverse('vault:browse'))
        self.assertEqual(Account.objects.count(), 2)
        self.assertEqual(Url.objects.count(), 2)
        self.assertEqual(Email.objects.count(), 2)

    def test_import_ignores_files_that_are_not_text(self):
        uploaded = SimpleUploadedFile(
            'image.png', b'\x89PNG\r\n\x1a\n junk', content_type='image/png'
        )
        response = self.client.post(
            reverse('vault:import'), {'folder': [uploaded]}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Account.objects.count(), 0)

    def test_import_reports_when_nothing_was_usable(self):
        uploaded = SimpleUploadedFile('junk.txt', b'nothing here\n',
                                      content_type='text/plain')
        response = self.client.post(reverse('vault:import'),
                                    {'file': uploaded}, follow=True)
        self.assertEqual(Account.objects.count(), 0)
        self.assertContains(response, 'No new records were created')

    def test_import_requires_a_file_a_folder_or_a_server_path(self):
        response = self.client.post(reverse('vault:import'), {})
        self.assertContains(response, 'Please choose a file, a folder, or a '
                                      'server path.')

    def test_import_reads_a_folder_that_lives_on_the_server(self):
        import os
        import tempfile

        folder = tempfile.mkdtemp()
        nested = os.path.join(folder, 'sub')
        os.makedirs(nested)
        with open(os.path.join(folder, 'one.txt'), 'w') as handle:
            handle.write('https://srv.example|u1|p1|127.0.0.1\n')
        with open(os.path.join(nested, 'two.txt'), 'w') as handle:
            handle.write('https://srv.example|u2|p2|127.0.0.2\n')
        with open(os.path.join(folder, 'ignore.me'), 'w') as handle:
            handle.write('https://srv.example|u3|p3|127.0.0.3\n')

        response = self.client.post(reverse('vault:import'),
                                    {'server_path': folder})
        self.assertRedirects(response, reverse('vault:browse'))
        self.assertEqual(Account.objects.count(), 2)
        self.assertEqual(User.objects.count(), 2)

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

    def test_bulk_import_rows_deduplicates_references(self):
        parsed = [
            {'url': 'https://a.com', 'account_type': 'email',
             'account_identifier': 'e@a.com', 'secret_data': 'pw',
             'ip_address': '1.1.1.1'},
            {'url': 'https://a.com', 'account_type': 'email',
             'account_identifier': 'e@a.com', 'secret_data': 'pw',
             'ip_address': '1.1.1.1'},
            {'url': 'https://b.com', 'account_type': 'user',
             'account_identifier': 'bob', 'secret_data': 'pw',
             'ip_address': 'N/A'},
            None,
        ]
        stats = bulk_import_rows(iter(parsed))
        created, skipped = stats  # still unpacks like the old return value
        self.assertEqual(created, 2)
        self.assertEqual(skipped, 1)
        self.assertEqual(stats.prepared, 2)  # the exact duplicate folded in
        self.assertEqual(stats.reused, 0)
        self.assertEqual(Account.objects.count(), 2)
        self.assertEqual(Url.objects.count(), 2)
        self.assertEqual(Pass.objects.filter(name='pw').count(), 1)
        self.assertEqual(Email.objects.filter(name='e@a.com').count(), 1)
        self.assertEqual(User.objects.filter(name='bob').count(), 1)
        self.assertEqual(Ip.objects.count(), 1)

    def test_bulk_import_lines_streams_and_measures_exactly(self):
        lines = [
            f'https://many.example|user{i}|shared-pw|10.0.0.{i % 3}'
            for i in range(1000)
        ]
        created, skipped = bulk_import_lines(lines, batch_size=100)
        self.assertEqual((created, skipped), (1000, 0))
        self.assertEqual(Url.objects.count(), 1)
        self.assertEqual(Pass.objects.count(), 1)
        self.assertEqual(Ip.objects.count(), 3)

        # Importing the same lines again stores nothing new.
        again = bulk_import_lines(lines, batch_size=100)
        self.assertEqual(again.created, 0)
        self.assertEqual(again.reused, 1000)
        self.assertEqual(Account.objects.count(), 1000)

    def test_overlong_values_are_rejected_instead_of_blowing_up(self):
        created, skipped = bulk_import_lines(
            ['https://x.example|' + 'u' * 300 + '|pw|N/A',
             'https://x.example|ok-user|pw|N/A']
        )
        self.assertEqual((created, skipped), (1, 1))

    def test_importfolder_command_ingests_a_directory(self):
        import os
        import tempfile

        tmp = tempfile.mkdtemp()
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

    def test_importfolder_dry_run_writes_nothing(self):
        import os
        import tempfile
        from io import StringIO

        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, 'one.txt'), 'w') as handle:
            handle.write(
                'https://dry.example|u1|pw|N/A\n'
                'this line is not a record at all\n'
            )
        out = StringIO()
        call_command('importfolder', tmp, '--dry-run', '--quiet',
                     stdout=out)
        output = out.getvalue()
        self.assertIn('Dry run', output)
        self.assertIn('1 would be stored', output)
        self.assertIn('1 would be skipped', output)
        self.assertEqual(Account.objects.count(), 0)


class ServerImportGuardTests(TestCase):
    def test_import_root_guard_rejects_other_folders(self):
        import os
        import tempfile

        folder = tempfile.mkdtemp()
        with open(os.path.join(folder, 'one.txt'), 'w') as handle:
            handle.write('https://guarded.example|u1|pw|N/A\n')

        with self.settings(IMPORT_ROOTS=[os.path.join(folder, 'elsewhere')]):
            response = self.client.post(reverse('vault:import'),
                                        {'server_path': folder})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'cannot be read')
        self.assertEqual(Account.objects.count(), 0)

    def test_import_root_guard_allows_a_listed_folder(self):
        import os
        import tempfile

        folder = tempfile.mkdtemp()
        with open(os.path.join(folder, 'one.txt'), 'w') as handle:
            handle.write('https://guarded.example|u1|pw|N/A\n')

        with self.settings(IMPORT_ROOTS=[folder]):
            response = self.client.post(reverse('vault:import'),
                                        {'server_path': folder})
        self.assertRedirects(response, reverse('vault:browse'))
        self.assertEqual(Account.objects.count(), 1)


class ReferenceTableTests(TestCase):
    def setUp(self):
        self.url = Url.objects.create(name='https://example.com/login')
        self.email = Email.objects.create(name='person@example.com')
        self.password = Pass.objects.create(name='hunter2')
        self.account = Account.objects.create(
            url=self.url, account_type='email', email=self.email,
            password=self.password,
        )

    def test_url_host_is_derived_from_the_name(self):
        created = Url.objects.create(name='https://Mail.Example.com:8443/in')
        self.assertEqual(created.host, 'mail.example.com')

    def test_reference_delete_is_blocked_while_in_use(self):
        response = self.client.post(
            reverse('vault:reference_delete', args=['email', self.email.pk]),
        )
        # Still in use by self.account, so it must not be deleted.
        self.assertEqual(Email.objects.filter(pk=self.email.pk).count(), 1)
        self.assertContains(response, 'still used by')

    def test_unused_reference_value_can_be_deleted(self):
        spare = Email.objects.create(name='spare@example.com')
        response = self.client.post(
            reverse('vault:reference_delete', args=['email', spare.pk]),
            follow=True,
        )
        self.assertFalse(Email.objects.filter(pk=spare.pk).exists())
        self.assertContains(response, 'deleted successfully')

    def test_deleting_a_record_keeps_the_shared_values(self):
        self.account.delete()
        self.assertTrue(Url.objects.filter(pk=self.url.pk).exists())
        self.assertTrue(Email.objects.filter(pk=self.email.pk).exists())
        self.assertTrue(Pass.objects.filter(pk=self.password.pk).exists())

    def test_reference_add_reuses_existing_value(self):
        for _ in range(2):
            self.client.post(
                reverse('vault:references', args=['email']),
                {'name': 'dup@example.com'},
            )
        # The same value is stored once and reused, never duplicated.
        self.assertEqual(
            Email.objects.filter(name='dup@example.com').count(), 1
        )

    def test_reference_list_reports_usage_and_filters(self):
        response = self.client.get(
            reverse('vault:references', args=['email'])
        )
        entity = list(response.context['entities'])[0]
        self.assertEqual(entity.usage, 1)

        response = self.client.get(
            reverse('vault:references', args=['email']), {'q': 'nope'}
        )
        self.assertEqual(response.context['total'], 0)
        self.assertContains(response, 'No Emails yet')

    def test_reference_list_paginates(self):
        Email.objects.bulk_create([
            Email(name=f'user{i}@many.example') for i in range(250)
        ])
        response = self.client.get(
            reverse('vault:references', args=['email'])
        )
        self.assertEqual(len(response.context['entities']), 100)
        self.assertEqual(response.context['total'], 251)
        self.assertContains(response, 'Page 1 of 3')


class LanguageTests(TestCase):
    def setUp(self):
        self.url = Url.objects.create(name='https://example.com/login')
        self.email = Email.objects.create(name='person@example.com')
        self.record = Account.objects.create(
            url=self.url, account_type='email', email=self.email,
            password=Pass.objects.create(name='test-secret'),
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
        # The new overview cards and the relationship table are translated.
        self.assertContains(response, 'الجداول المرجعية')
        self.assertContains(response, 'بحث متقدم')

        translated_pages = (
            ('vault:browse', (), 'كل السجلات'),
            ('vault:search', (), 'البحث في السجلات'),
            ('vault:advanced_search', (), 'بحث متقدم'),
            ('vault:import', (), 'استيراد السجلات'),
            ('vault:references_index', (), 'الجداول المرجعية'),
            ('vault:references', ('email',), 'عناوين البريد الإلكتروني'),
            ('vault:detail', (self.record.pk,), 'تفاصيل السجل'),
            ('vault:update', (self.record.pk,), 'تعديل السجل'),
            ('vault:delete', (self.record.pk,), 'هل تريد حذف هذا السجل؟'),
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

        # A search result page keeps its translated chrome and its exact-match
        # note, even when the matched value itself is Latin text.
        response = self.client.get(
            reverse('vault:search'), {'field': 'domain', 'q': 'example.com'}
        )
        self.assertContains(response, 'سجل واحد مطابق')
        self.assertContains(response, 'تصدير الكل بصيغة CSV')

        csv_response = self.client.get(reverse('vault:export_csv'))
        body = stream_body(csv_response).decode('utf-8-sig')
        # Translated header row, and the derived host is exported too.
        self.assertIn('النطاق', body)
        self.assertIn('example.com', body)

        response = self.client.post(
            reverse('set_language'),
            {'language': 'en', 'next': reverse('vault:dashboard')},
        )
        self.assertRedirects(response, reverse('vault:dashboard'))
        self.assertContains(
            self.client.get(reverse('vault:dashboard')), 'Your workspace'
        )
