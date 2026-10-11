"""
Administrator sign-in for the LogIntel workspace.

Accounts come from Django's built-in ``auth_user`` table. Only active users
with ``is_staff`` set may use the workspace; the login form refuses any other
account after the password has been checked.
"""

from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.http import HttpResponseRedirect
from django.utils.translation import gettext_lazy as _


def is_administrator(user):
    """True when `user` is signed in, active and an administrator (staff)."""
    return bool(
        user.is_authenticated and user.is_active and user.is_staff
    )


class AdministratorLoginForm(AuthenticationForm):
    error_messages = {
        **AuthenticationForm.error_messages,
        'not_administrator': _(
            'This account does not have administrator access.'
        ),
    }

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)  # rejects inactive accounts
        if not user.is_staff:
            raise ValidationError(
                self.error_messages['not_administrator'],
                code='not_administrator',
            )


class AdministratorLoginView(LoginView):
    template_name = 'vault/login.html'
    authentication_form = AdministratorLoginForm
    redirect_authenticated_user = False  # handled in get() below

    def get(self, request, *args, **kwargs):
        # An administrator who is already signed in goes straight through.
        # Other signed-in users see the form, so they cannot loop between
        # this page and the workspace.
        if is_administrator(request.user):
            return HttpResponseRedirect(self.get_success_url())
        return super().get(request, *args, **kwargs)
