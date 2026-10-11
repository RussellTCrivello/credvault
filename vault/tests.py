from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .models import Record


class WorkspaceInterfaceTests(TestCase):
    def setUp(self):
        self.record = Record.objects.create(
            domain='example.com',
            url='https://example.com/login',
            account_type='email',
            account_identifier='person@example.com',
            secret_data='test-secret',
            ip_address='203.0.113.10',
        )

    def test_workspace_pages_render(self):
        pages = (
            ('vault:dashboard', 'Your workspace'),
            ('vault:browse', 'All records'),
            ('vault:search', 'Search records'),
            ('vault:add', 'Add a record'),
            ('vault:import', 'Import records'),
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

    def test_add_record_saves_and_redirects_to_details(self):
        response = self.client.post(
            reverse('vault:add'),
            {
                'domain': 'new.example',
                'url': 'https://new.example/login',
                'account_type': 'user',
                'account_identifier': 'new-user',
                'secret_data': 'new-secret',
                'ip_address': 'N/A',
            },
        )

        self.assertRedirects(
            response,
            reverse('vault:detail', args=[self.record.id + 1]),
        )
        self.assertTrue(Record.objects.filter(domain='new.example').exists())

    def test_search_results_link_to_record_without_listing_secret(self):
        response = self.client.get(
            reverse('vault:search'),
            {'keyword': 'test-secret'},
        )

        self.assertContains(response, 'example.com')
        self.assertNotContains(response, '<td>test-secret</td>')

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
        self.assertTrue(Record.objects.filter(domain='import.example').exists())

    def test_delete_requires_post_confirmation(self):
        response = self.client.post(
            reverse('vault:delete', args=[self.record.id]),
        )

        self.assertRedirects(response, reverse('vault:browse'))
        self.assertFalse(Record.objects.filter(id=self.record.id).exists())

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
        self.assertContains(response, 'النطاق')
        self.assertContains(response, 'اسم المستخدم')

        csv_response = self.client.get(reverse('vault:export_csv'))
        self.assertIn('النطاق'.encode(), csv_response.content)

        response = self.client.post(
            reverse('set_language'),
            {'language': 'en', 'next': reverse('vault:dashboard')},
        )
        self.assertRedirects(response, reverse('vault:dashboard'))
        self.assertContains(self.client.get(reverse('vault:dashboard')), 'Your workspace')
