from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

PASSWORD = 'correct-horse-battery'


class AdministratorLoginTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            'admin', 'admin@example.com', PASSWORD, is_staff=True
        )
        self.staff_user = User.objects.create_user(
            'staffer', 'staffer@example.com', PASSWORD,
            is_staff=True, is_active=True,
        )
        self.plain_user = User.objects.create_user(
            'viewer', 'viewer@example.com', PASSWORD
        )
        self.inactive_admin = User.objects.create_user(
            'retired', 'retired@example.com', PASSWORD,
            is_staff=True, is_active=False,
        )
        self.login_url = reverse('vault:login')

    # --- Anonymous visitors are sent to the login page -------------------

    def test_workspace_pages_redirect_anonymous_visitors_to_login(self):
        pages = (
            reverse('vault:dashboard'),
            reverse('vault:browse'),
            reverse('vault:search'),
            reverse('vault:add'),
            reverse('vault:import'),
            reverse('vault:references_index'),
            reverse('vault:export_csv'),
        )
        for page in pages:
            with self.subTest(page=page):
                response = self.client.get(page)
                self.assertRedirects(
                    response,
                    f'{self.login_url}?next={page}',
                    fetch_redirect_response=False,
                )

    def test_record_detail_redirects_anonymous_visitors(self):
        from .models import Account, Pass, Url
        record = Account.objects.create(
            url=Url.objects.create(name='https://example.com/login'),
            account_type='email',
            password=Pass.objects.create(name='secret-1'),
        )
        response = self.client.get(reverse('vault:detail', args=[record.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith(self.login_url))

    def test_login_page_is_open_to_anonymous_visitors(self):
        response = self.client.get(self.login_url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'vault/login.html')
        self.assertContains(response, 'name="username"')
        self.assertContains(response, 'name="password"')

    def test_django_admin_site_is_not_hijacked(self):
        response = self.client.get('/admin/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response['Location'])

    # --- Signing in -------------------------------------------------------

    def test_administrator_can_sign_in_and_reach_the_workspace(self):
        response = self.client.post(self.login_url, {
            'username': 'admin', 'password': PASSWORD,
        })
        self.assertRedirects(
            response, reverse('vault:dashboard'), fetch_redirect_response=False
        )
        page = self.client.get(reverse('vault:browse'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Signed in as')
        self.assertContains(page, 'admin')

    def test_sign_in_returns_visitor_to_the_page_they_asked_for(self):
        target = reverse('vault:browse')
        response = self.client.post(
            f'{self.login_url}?next={target}',
            {'username': 'admin', 'password': PASSWORD, 'next': target},
        )
        self.assertRedirects(response, target, fetch_redirect_response=False)

    def test_external_next_url_is_ignored(self):
        response = self.client.post(f'{self.login_url}?next=https://evil.example.com/', {
            'username': 'admin', 'password': PASSWORD,
            'next': 'https://evil.example.com/',
        })
        self.assertRedirects(
            response, reverse('vault:dashboard'), fetch_redirect_response=False
        )

    def test_wrong_password_is_refused(self):
        response = self.client.post(self.login_url, {
            'username': 'admin', 'password': 'wrong',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Please enter a correct username and password')
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(
            self.client.get(reverse('vault:dashboard')).status_code, 302
        )

    def test_ordinary_user_is_refused_even_with_correct_password(self):
        response = self.client.post(self.login_url, {
            'username': 'viewer', 'password': PASSWORD,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'does not have administrator access')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_deactivated_administrator_is_refused(self):
        response = self.client.post(self.login_url, {
            'username': 'retired', 'password': PASSWORD,
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_signed_in_ordinary_user_is_sent_back_to_login(self):
        self.client.login(username='viewer', password=PASSWORD)
        response = self.client.get(reverse('vault:dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith(self.login_url))
        # The login page must not bounce them back again (no redirect loop).
        login_page = self.client.get(self.login_url)
        self.assertEqual(login_page.status_code, 200)

    def test_signed_in_administrator_visiting_login_is_forwarded(self):
        self.client.login(username='admin', password=PASSWORD)
        response = self.client.get(self.login_url)
        self.assertRedirects(
            response, reverse('vault:dashboard'), fetch_redirect_response=False
        )

    # --- Signing out ------------------------------------------------------

    def test_logout_requires_post_and_ends_the_session(self):
        self.client.login(username='admin', password=PASSWORD)
        self.assertEqual(self.client.get(reverse('vault:logout')).status_code, 405)
        self.assertEqual(
            self.client.get(reverse('vault:dashboard')).status_code, 200
        )

        response = self.client.post(reverse('vault:logout'))
        self.assertRedirects(
            response, self.login_url, fetch_redirect_response=False
        )
        self.assertEqual(
            self.client.get(reverse('vault:dashboard')).status_code, 302
        )

    def test_sidebar_shows_logout_button_only_when_signed_in(self):
        self.client.login(username='admin', password=PASSWORD)
        self.assertContains(self.client.get(reverse('vault:dashboard')), 'Log out')

    def test_language_switch_stays_reachable_for_anonymous_visitors(self):
        response = self.client.post('/i18n/setlang/', {'language': 'ar'})
        self.assertNotIn(self.login_url, response.get('Location', ''))
