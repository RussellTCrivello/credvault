from django.conf import settings
from django.contrib.auth.views import redirect_to_login

from .access import is_administrator


class AdministratorLoginMiddleware:
    """
    Only signed-in administrators may use the workspace.

    Anything else is redirected to the administrator login page, with the
    originally requested path kept in ``next``. These paths stay open:

    * the login and logout pages themselves,
    * the language switcher (``/i18n/``),
    * static and media files,
    * Django's own ``/admin/`` site, which applies its own staff check.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.public_prefixes = (
            '/login/',
            '/logout/',
            '/i18n/',
            '/admin/',
            settings.STATIC_URL or '/static/',
            settings.MEDIA_URL or '/media/',
        )

    def __call__(self, request):
        if request.path_info.startswith(self.public_prefixes):
            return self.get_response(request)
        if is_administrator(request.user):
            return self.get_response(request)
        return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
