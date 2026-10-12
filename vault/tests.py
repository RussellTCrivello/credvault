import json
import os
import shutil
import tempfile
import time
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

from .importer import (
    DEFAULT_STAGE_ROWS, BulkImportSession, bulk_import_lines, stage_rows,
    worker_count,
)
from .models import Account, Email, ImportJob, Ip, Pass, Url, User
from .utils import parse_line


def latest_import_job():
    """The most recently created ImportJob (highest pk)."""
    return ImportJob.objects.order_by('pk').last()


class WorkspaceInterfaceTests(TestCase):
    def setUp(self):
        administrator = get_user_model().objects.create_user(
            username='workspace-admin',
            password='test-password',
            is_staff=True,
        )
        self.client.force_login(administrator)

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

    def test_anonymous_users_are_redirected_to_login(self):
        response = Client().get(reverse('vault:dashboard'))

        self.assertRedirects(
            response,
            f"{reverse('vault:login')}?next={reverse('vault:dashboard')}",
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
                if route_name == 'vault:import':
                    self.assertContains(response, 'data-import-progress')
                    self.assertContains(response, 'data-import-step="complete"')
                    self.assertContains(response, 'data-import-percent')

    def test_profile_menu_groups_account_and_preferences(self):
        response = self.client.get(reverse('vault:references_index'))

        self.assertContains(response, 'id="profile-trigger"')
        self.assertContains(response, 'popovertarget="profile-popover"')
        self.assertContains(response, 'id="profile-popover"')
        self.assertContains(response, 'workspace-admin')
        self.assertContains(response, 'name="language" value="en"')
        self.assertContains(response, 'data-theme-choice="dark"')
        self.assertContains(response, reverse('vault:logout'))
        self.assertNotContains(response, 'class="account-box"')
        self.assertNotContains(response, 'id="theme-toggle"')

    def test_search_filters_have_visible_field_labels(self):
        for route_name in ('vault:browse', 'vault:search'):
            with self.subTest(page=route_name):
                response = self.client.get(reverse(route_name))
                self.assertContains(response, 'Search in')
                self.assertContains(response, 'Exact value')

        response = self.client.get(reverse('vault:advanced_search'))
        self.assertContains(response, 'advanced-search-form')
        self.assertContains(response, 'URL contains')
        self.assertContains(response, 'Created from')

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

    def test_advanced_search_matches_substrings(self):
        # Advanced search (per-field filters and the keyword) matches
        # anywhere inside a value, case-insensitively -- the "contains"
        # behaviour its labels promise, backed by the pg_trgm indexes.
        inner_url = Url.objects.create(name='https://vpn.corp.example.net/remote')
        Account.objects.create(
            url=inner_url,
            account_type='user',
            user=User.objects.create(name='svc.backup'),
            password=Pass.objects.create(name='Tr0ub4dor&3'),
            ip=self.ip,
        )

        cases = [
            {'keyword': 'CORP'},       # keyword, middle of the URL, other case
            {'url': '.corp.'},         # URL filter, mid-string token
            {'username': 'vc.back'},   # username filter, mid-string
            {'password': '0ub4d'},     # password filter, mid-string
        ]
        for params in cases:
            with self.subTest(params=params):
                response = self.client.get(
                    reverse('vault:advanced_search'), params
                )
                self.assertContains(response, 'svc.backup')

    def test_quick_search_stays_exact_for_non_domain_values(self):
        # Quick search is the exact-match tier: a partial value must not
        # match on any field (see README, "Search semantics").
        partial_values = [
            {'field': 'email', 'q': 'erson@example'},
            {'field': 'password', 'q': 'test-secr'},
            {'field': 'ip', 'q': '203.0.113'},
        ]
        for params in partial_values:
            with self.subTest(params=params):
                response = self.client.get(reverse('vault:search'), params)
                self.assertContains(response, 'No matching records')

                # The full value still finds the record.
                full = params.copy()
                full['q'] = {
                    'email': 'person@example.com',
                    'password': 'test-secret',
                    'ip': '203.0.113.10',
                }[params['field']]
                response = self.client.get(reverse('vault:search'), full)
                self.assertContains(response, 'person@example.com')

    def test_search_with_many_matching_urls_returns_them(self):
        urls = Url.objects.bulk_create([
            Url(name=f'https://bulk-search.example/login/{index}')
            for index in range(100)
        ])
        Account.objects.create(
            url=urls[0],
            account_type='email',
            email=self.email,
            password=self.password,
            ip=self.ip,
        )

        responses = (
            self.client.get(
                reverse('vault:advanced_search'),
                {'keyword': 'bulk-search'},
            ),
            self.client.get(
                reverse('vault:search'),
                {'field': 'domain', 'q': 'bulk-search.example'},
            ),
        )

        for response in responses:
            with self.subTest(path=response.request['PATH_INFO']):
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'bulk-search.example')

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

        # The import lands on the durable job's detail page, where its
        # state and progress can be inspected (README, "Import jobs").
        job = latest_import_job()
        detail_url = reverse('vault:import_job_detail', args=[job.job_id])
        self.assertRedirects(response, detail_url)

        page = self.client.get(detail_url)
        self.assertContains(page, 'Import job details')
        self.assertContains(page, job.source_name)
        self.assertContains(page, 'Completed')
        self.assertContains(page, '100.0%')

        job.refresh_from_db()
        self.assertEqual(job.status, 'completed')
        self.assertEqual(job.total_committed, 1)
        self.assertTrue(
            Url.objects.filter(name='https://import.example/login').exists()
        )
        self.assertTrue(
            Account.objects.filter(user__name='import-user').exists()
        )

    def test_import_progress_reports_exact_lines_for_uploaded_folders(self):
        first_file = SimpleUploadedFile(
            'first.txt',
            b'# comment\nhttps://progress.example/login|progress-user|secret\n',
            content_type='text/plain',
        )
        second_file = SimpleUploadedFile(
            'second.txt',
            b'invalid line\nhttps://progress.example/login|other-user|secret',
            content_type='text/plain',
        )

        response = self.client.post(
            reverse('vault:import'),
            {'folder': [first_file, second_file]},
            HTTP_X_IMPORT_PROGRESS='1',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.streaming)
        events = [
            json.loads(chunk.decode('utf-8'))
            for chunk in response.streaming_content
        ]
        progress_events = [
            event for event in events if event['type'] == 'progress'
        ]
        count_events = [
            event for event in events if event['type'] == 'count_progress'
        ]
        self.assertEqual(progress_events[0]['total'], 4)
        self.assertEqual(progress_events[-1]['processed'], 4)
        self.assertEqual(count_events[-1]['files_scanned'], 2)
        self.assertEqual(count_events[-1]['total_files'], 2)
        self.assertEqual(events[-1]['type'], 'done')
        self.assertEqual(events[-1]['created'], 2)
        self.assertEqual(events[-1]['skipped'], 2)
        self.assertEqual(events[-1]['files'], 2)
        self.assertEqual(
            Account.objects.filter(url__name='https://progress.example/login')
            .count(),
            2,
        )

    def test_import_progress_counts_lines_from_folder_paths(self):
        tmp_dir = tempfile.mkdtemp()
        try:
            nested_dir = os.path.join(tmp_dir, 'nested')
            os.makedirs(nested_dir, exist_ok=True)
            with open(os.path.join(tmp_dir, 'root.txt'), 'w') as handle:
                handle.write('https://path-progress.example/login|one|secret\n')
                handle.write('invalid line\n')
            with open(os.path.join(nested_dir, 'nested.txt'), 'w') as handle:
                handle.write(
                    'https://path-progress.example/login|two|secret'
                )

            response = self.client.post(
                reverse('vault:import'),
                {'folder_path': tmp_dir},
                HTTP_X_IMPORT_PROGRESS='1',
            )
            events = [
                json.loads(chunk.decode('utf-8'))
                for chunk in response.streaming_content
            ]

            progress_events = [
                event for event in events if event['type'] == 'progress'
            ]
            self.assertEqual(progress_events[-1]['total'], 3)
            self.assertEqual(progress_events[-1]['processed'], 3)
            self.assertEqual(events[-1]['created'], 2)
            self.assertEqual(events[-1]['skipped'], 1)
            self.assertEqual(events[-1]['files'], 2)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

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
        # One job for the whole upload; it lands on that job's detail page.
        job = latest_import_job()
        self.assertRedirects(
            response, reverse('vault:import_job_detail', args=[job.job_id])
        )
        self.assertEqual(
            Account.objects.filter(url__name='https://folder.example/login').count(),
            2,
        )
        # Both files are accounted for in one ledger: 2 committed records
        # and 2 skipped lines (the comment and the invalid line).
        self.assertEqual(job.total_committed, 2)
        self.assertEqual(job.total_skipped, 2)
        self.assertEqual(job.records.count(), 4)

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
            job = latest_import_job()
            self.assertRedirects(
                response, reverse('vault:import_job_detail', args=[job.job_id])
            )
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

    def test_import_accepts_multiple_folder_paths(self):
        # One path per line in the folder_path field: every listed folder is
        # imported in the same job.
        dir_one = tempfile.mkdtemp()
        dir_two = tempfile.mkdtemp()
        try:
            with open(os.path.join(dir_one, 'one.txt'), 'w') as fh:
                fh.write('https://multi1.example/login|multi-user1|pw1|10.2.0.1\n')
            with open(os.path.join(dir_two, 'two.txt'), 'w') as fh:
                fh.write('https://multi2.example/login|multi-user2|pw2|10.2.0.2\n')

            response = self.client.post(
                reverse('vault:import'),
                {'folder_path': f'{dir_one}\n{dir_two}'},
            )
            # Two paths -> two durable jobs -> the job list, where both
            # jobs are visible with their state and progress.
            self.assertRedirects(response, reverse('vault:import_jobs'))
            page = self.client.get(reverse('vault:import_jobs'))
            self.assertContains(page, 'Import jobs')
            jobs = list(ImportJob.objects.order_by('pk'))
            self.assertEqual(len(jobs), 2)
            for job in jobs:
                # The list shows each job's (truncated) id and source.
                self.assertContains(page, str(job.job_id)[:8])
                self.assertContains(page, job.source_name)
                self.assertContains(
                    page,
                    reverse('vault:import_job_detail', args=[job.job_id]),
                )
            self.assertEqual(
                Account.objects.filter(
                    url__name='https://multi1.example/login'
                ).count(),
                1,
            )
            self.assertEqual(
                Account.objects.filter(
                    url__name='https://multi2.example/login'
                ).count(),
                1,
            )
        finally:
            shutil.rmtree(dir_one, ignore_errors=True)
            shutil.rmtree(dir_two, ignore_errors=True)

    def test_import_page_accepts_several_files_at_once(self):
        # The picker offers the `multiple` attribute and one request stores
        # every selected file (the view reads request.FILES.getlist('file')).
        response = self.client.get(reverse('vault:import'))
        html = response.content.decode()
        file_input = html[html.index('name="file"'):].split('>')[0]
        self.assertIn('multiple', file_input)

        uploads = [
            SimpleUploadedFile(
                f'many-{index}.txt',
                f'https://many{index}.example/login|many-user{index}'
                f'|many-pw{index}|10.3.0.{index}\n'.encode(),
                content_type='text/plain',
            )
            for index in (1, 2, 3)
        ]
        response = self.client.post(reverse('vault:import'), {'file': uploads})
        job = latest_import_job()
        self.assertRedirects(
            response, reverse('vault:import_job_detail', args=[job.job_id])
        )
        # All three files share one job and one ledger: positions (and
        # therefore idempotency keys) must not collide across files.
        self.assertEqual(job.records.count(), 3)
        self.assertEqual(
            sorted(job.records.values_list('source_position', flat=True)),
            [1, 2, 3],
        )
        self.assertEqual(job.total_committed, 3)
        for index in (1, 2, 3):
            self.assertEqual(
                Account.objects.filter(
                    url__name=f'https://many{index}.example/login'
                ).count(),
                1,
            )

    def test_import_reports_a_missing_path(self):
        good_dir = tempfile.mkdtemp()
        try:
            with open(os.path.join(good_dir, 'good.txt'), 'w') as fh:
                fh.write('https://good.example/login|good-user|pw|10.2.1.1\n')

            missing = os.path.join(good_dir, 'does-not-exist')
            response = self.client.post(
                reverse('vault:import'),
                {'folder_path': f'{good_dir}\n{missing}'},
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'does not exist')
            self.assertContains(response, 'does-not-exist')
            # Nothing was imported when any listed path is missing.
            self.assertEqual(Account.objects.count(), 1)

        finally:
            shutil.rmtree(good_dir, ignore_errors=True)

    def test_maximum_speed_import_rebuilds_search_indexes(self):
        trigram_indexes = (
            "select count(*) from pg_indexes "
            "where indexname like 'vault\\_%\\_name\\_trgm'"
        )
        with connection.cursor() as cursor:
            cursor.execute(trigram_indexes)
            self.assertEqual(cursor.fetchone()[0], 5)  # migration 0003

        uploaded_file = SimpleUploadedFile(
            'fast.txt',
            b'https://fast.example/login|fast-user|fast-pw|10.2.2.1\n',
            content_type='text/plain',
        )
        response = self.client.post(
            reverse('vault:import'),
            {'file': uploaded_file, 'fast': 'on'},
        )
        job = latest_import_job()
        self.assertRedirects(
            response, reverse('vault:import_job_detail', args=[job.job_id])
        )
        self.assertEqual(
            Account.objects.filter(
                url__name='https://fast.example/login'
            ).count(),
            1,
        )
        # The five search indexes are back after the import.
        with connection.cursor() as cursor:
            cursor.execute(trigram_indexes)
            self.assertEqual(cursor.fetchone()[0], 5)

    def test_bulk_session_dedupes_across_batches(self):
        # A tiny batch size forces many flushes; duplicates that span batch
        # boundaries must still collapse onto single rows.
        before = {
            model: model.objects.count()
            for model in (Account, Url, Pass, User)
        }
        session = BulkImportSession(batch_rows=7, workers=1)
        session.add_parsed(
            parse_line(
                f'https://batch{i % 3}.example.com/x|user{i}|pw{i % 2}|1.2.3.{i % 5}'
            )
            for i in range(40)
        )
        created, skipped = session.finish()
        self.assertEqual((created, skipped), (40, 0))
        self.assertEqual(Account.objects.count() - before[Account], 40)
        self.assertEqual(Url.objects.count() - before[Url], 3)
        self.assertEqual(Pass.objects.count() - before[Pass], 2)
        self.assertEqual(User.objects.count() - before[User], 40)

    def test_batch_size_override_from_the_environment(self):
        # The batch knobs are the ones the README tells big machines to turn.
        with mock.patch.dict(os.environ, {'VAULT_IMPORT_BATCH_ROWS': '15000'}):
            self.assertEqual(stage_rows(), 15000)
        with mock.patch.dict(os.environ, {'VAULT_IMPORT_BATCH_ROWS': '1'}):
            self.assertEqual(stage_rows(), 10000)  # floor of the override
        with mock.patch.dict(os.environ, {'VAULT_IMPORT_BATCH_ROWS': 'lots'}):
            self.assertGreaterEqual(stage_rows(), DEFAULT_STAGE_ROWS)

    def test_parse_workers_store_the_same_records_as_inline_parsing(self):
        lines = [
            f'https://workers{i % 4}.example.com/x|wuser{i}@workers.example'
            f'|wpw{i % 3}|10.2.3.{i % 2}'
            for i in range(250)
        ]

        session = BulkImportSession(workers=1, batch_rows=60)
        session.add_lines(line + '\n' for line in lines)
        inline_created, inline_skipped = session.finish()

        Account.objects.all().delete()
        for model in (Url, Email, User, Pass, Ip):
            model.objects.all().delete()

        session = BulkImportSession(workers=2, batch_rows=60)
        session.add_lines(line + '\n' for line in lines)
        worker_created, worker_skipped = session.finish()

        self.assertEqual(
            (worker_created, worker_skipped), (inline_created, inline_skipped)
        )
        self.assertEqual((worker_created, worker_skipped), (250, 0))
        self.assertEqual(Account.objects.count(), 250)
        for account in Account.objects.all():
            self.assertEqual(account.compute_fingerprint(), account.fingerprint)

    def test_parse_pool_prefers_fork_and_falls_back_to_spawn(self):
        # Windows offers no 'fork' start method; the pool must be built
        # from the contexts the platform actually supports (the old code
        # asked for 'fork' unconditionally and raised ValueError there).
        from .importer import _parse_context

        with mock.patch(
            'multiprocessing.get_all_start_methods',
            return_value=['spawn'],
        ):
            self.assertEqual(_parse_context().get_start_method(), 'spawn')
        with mock.patch(
            'multiprocessing.get_all_start_methods',
            return_value=['fork', 'spawn'],
        ):
            self.assertEqual(_parse_context().get_start_method(), 'fork')

    def test_spawn_workers_store_the_same_records_as_inline_parsing(self):
        # The exact code path Windows takes (spawn pool), forced on any
        # platform: parsing equivalence, batching and cleanup must hold.
        lines = [
            f'https://spawn{i % 3}.example.com/x|suser{i}@spawn.example'
            f'|spw{i % 2}|10.4.5.{i % 7}'
            for i in range(60)
        ]

        with mock.patch(
            'multiprocessing.get_all_start_methods',
            return_value=['spawn'],
        ):
            session = BulkImportSession(workers=2, batch_rows=15)
            session.add_lines(line + '\n' for line in lines)
            created, skipped = session.finish()
        # The pool was really closed by finish(); no stragglers remain.
        self.assertIsNone(session._pool)
        self.assertEqual((created, skipped), (60, 0))
        self.assertEqual(
            Account.objects.filter(
                url__name__contains='spawn'
            ).count(),
            60,
        )
        for account in Account.objects.filter(url__name__contains='spawn'):
            self.assertEqual(account.compute_fingerprint(), account.fingerprint)

    def test_worker_count_honors_explicit_configuration_everywhere(self):
        # An explicit VAULT_IMPORT_WORKERS setting is respected even where
        # fork is unavailable; without it, non-POSIX platforms stay on the
        # safe inline default.
        with mock.patch('os.name', 'nt'), mock.patch.dict(
            os.environ, {'VAULT_IMPORT_WORKERS': '3'}
        ):
            self.assertEqual(worker_count(), 3)
        with mock.patch('os.name', 'nt'), mock.patch.dict(
            os.environ, {'VAULT_IMPORT_WORKERS': '0'}
        ):
            self.assertEqual(worker_count(), 1)
        with mock.patch.dict(os.environ, {'VAULT_IMPORT_WORKERS': 'lots'}):
            self.assertGreaterEqual(worker_count(), 1)

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
        self.assertContains(response, 'id="profile-popover"')
        self.assertContains(response, 'data-theme-choice="dark"')
        self.assertContains(response, 'تسجيل الخروج')

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
