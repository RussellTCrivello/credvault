from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from .models import (
    Account, Email, Ip, Pass, Url, User,
)
from .validators import (
    CANONICALIZERS, ERROR_MESSAGES, FORMAT_HINTS, MAX_EMAIL_LENGTH,
    MAX_IP_LENGTH, MAX_LENGTHS, MAX_PASSWORD_LENGTH, MAX_URL_LENGTH,
    MAX_USERNAME_LENGTH, EmailFormatValidator, IPAddressFormatValidator,
    PasswordFormatValidator, URLFormatValidator, UsernameFormatValidator,
)


class AccountForm(forms.ModelForm):
    """
    Captures a "login" (Account) by its component names instead of raw
    foreign keys. On save each component is resolved with get_or_create,
    so an existing URL / email / username / password / IP is reused
    instead of being entered a second time.

    Every component is checked against the format of its kind before it is
    stored, and stored in canonical form (see vault.validators), so
    ``HTTPS://Example.COM/login`` and ``https://example.com/login`` reuse the
    same row instead of creating a second one.

    All fields are configured as searchable and selectable drop-down
    comboboxes with live filtering and selection.
    """

    url_name = forms.CharField(
        label=_('URL'),
        max_length=MAX_URL_LENGTH,
        validators=[URLFormatValidator()],
        help_text=FORMAT_HINTS['url'],
        widget=forms.TextInput(attrs={
            'class': 'form-control combobox-input',
            'dir': 'ltr',
            'autocomplete': 'off',
            'data-combobox': 'url',
            'data-list-id': 'url-list',
            'placeholder': _('Search or select URL (e.g. https://example.com/login)'),
        }),
    )
    email_name = forms.CharField(
        label=_('Email'),
        required=False,
        max_length=MAX_EMAIL_LENGTH,
        validators=[EmailFormatValidator()],
        help_text=FORMAT_HINTS['email'],
        widget=forms.TextInput(attrs={
            'class': 'form-control combobox-input',
            'dir': 'ltr',
            'autocomplete': 'off',
            'data-combobox': 'email',
            'data-list-id': 'email-list',
            'placeholder': _('Search or select email (e.g. user@example.com)'),
        }),
    )
    user_name = forms.CharField(
        label=_('Username'),
        max_length=MAX_USERNAME_LENGTH,
        required=False,
        validators=[UsernameFormatValidator()],
        help_text=FORMAT_HINTS['user'],
        widget=forms.TextInput(attrs={
            'class': 'form-control combobox-input',
            'dir': 'auto',
            'autocomplete': 'off',
            'data-combobox': 'user',
            'data-list-id': 'user-list',
            'placeholder': _('Search or select username'),
        }),
    )
    password_name = forms.CharField(
        label=_('Password'),
        max_length=MAX_PASSWORD_LENGTH,
        validators=[PasswordFormatValidator()],
        help_text=FORMAT_HINTS['pass'],
        widget=forms.TextInput(attrs={
            'class': 'form-control combobox-input',
            'dir': 'auto',
            'autocomplete': 'off',
            'data-combobox': 'pass',
            'data-list-id': 'pass-list',
            'placeholder': _('Search or select password or secret'),
        }),
    )
    ip_name = forms.CharField(
        label=_('IP address'),
        max_length=MAX_IP_LENGTH,
        required=False,
        validators=[IPAddressFormatValidator()],
        help_text=FORMAT_HINTS['ip'],
        widget=forms.TextInput(attrs={
            'class': 'form-control combobox-input',
            'dir': 'ltr',
            'autocomplete': 'off',
            'data-combobox': 'ip',
            'data-list-id': 'ip-list',
            'placeholder': _('Search or select IP address (or N/A)'),
        }),
    )

    class Meta:
        model = Account
        fields = ['account_type']
        widgets = {
            'account_type': forms.Select(attrs={
                'class': 'form-control combobox-select',
                'data-combobox-select': 'true',
            }),
        }

    field_order = [
        'account_type',
        'url_name',
        'email_name',
        'user_name',
        'password_name',
        'ip_name',
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = kwargs.get('instance') or getattr(self, 'instance', None)
        if instance and instance.pk:
            self.fields['url_name'].initial = instance.url.name
            if instance.email:
                self.fields['email_name'].initial = instance.email.name
            if instance.user:
                self.fields['user_name'].initial = instance.user.name
            self.fields['password_name'].initial = instance.password.name
            if instance.ip:
                self.fields['ip_name'].initial = instance.ip.name

    def clean(self):
        """
        Check the identifier the account type asks for and store every
        component in canonical form, so that looking an existing value up by
        name finds it however it was typed.
        """
        cleaned = super().clean()
        account_type = cleaned.get('account_type')

        # A field whose format was rejected is absent from cleaned_data and
        # already carries its own error message; only report "required" or
        # "not allowed" for fields that are present and well formed.
        canonical = {
            'url_name': CANONICALIZERS['url'](cleaned.get('url_name')),
            'password_name': CANONICALIZERS['pass'](
                cleaned.get('password_name')
            ),
            'email_name': CANONICALIZERS['email'](cleaned.get('email_name')),
            'user_name': CANONICALIZERS['user'](cleaned.get('user_name')),
            'ip_name': CANONICALIZERS['ip'](cleaned.get('ip_name')),
        }
        for field, value in canonical.items():
            cleaned[field] = value or ''

        def report(field, message):
            if not self.errors.get(field):
                self.add_error(field, message)

        if account_type == 'email':
            if not canonical['email_name']:
                report(
                    'email_name',
                    _('An email address is required for email accounts.'),
                )
            if canonical['user_name']:
                report(
                    'user_name',
                    _('Email accounts cannot have a username.'),
                )
        else:
            if not canonical['user_name']:
                report(
                    'user_name',
                    _('A username is required for user accounts.'),
                )
            if canonical['email_name']:
                report(
                    'email_name',
                    _('User accounts cannot have an email.'),
                )
        return cleaned

    def save(self, commit=True):
        cleaned = self.cleaned_data
        account_type = cleaned['account_type']

        # Every value is already canonical, so get_or_create reuses the
        # existing row instead of storing a second spelling of it.
        url, _created = Url.objects.get_or_create(name=cleaned['url_name'])
        password, _created = Pass.objects.get_or_create(
            name=cleaned['password_name']
        )

        ip = None
        if cleaned.get('ip_name'):
            ip, _created = Ip.objects.get_or_create(name=cleaned['ip_name'])

        email = None
        user = None
        if account_type == 'email':
            email, _created = Email.objects.get_or_create(
                name=cleaned['email_name']
            )
        else:
            user, _created = User.objects.get_or_create(
                name=cleaned['user_name']
            )

        self.instance.url = url
        self.instance.account_type = account_type
        self.instance.email = email
        self.instance.user = user
        self.instance.password = password
        self.instance.ip = ip

        # Duplicate guard (mirrors Account.save for a friendly form error).
        fingerprint = self.instance.compute_fingerprint()
        duplicates = Account.objects.filter(fingerprint=fingerprint)
        if self.instance.pk:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise ValidationError(
                    _('This exact relationship already exists.')
                )

        return super().save(commit=commit)


class ReferenceForm(forms.Form):
    """
    Add a single value to one of the reference tables.

    The `kind` ('url', 'email', 'user', 'pass' or 'ip') selects the format
    the value is checked against and the canonical spelling it is stored
    in, so a value that already exists is reported as reused rather than
    added a second time.
    """

    name = forms.CharField(
        label=_('Name'),
        max_length=MAX_URL_LENGTH,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': _('Enter a value'),
        }),
    )

    def __init__(self, *args, kind=None, **kwargs):
        self.kind = kind if kind in CANONICALIZERS else None
        super().__init__(*args, **kwargs)
        if self.kind:
            field = self.fields['name']
            field.max_length = MAX_LENGTHS[self.kind]
            field.help_text = FORMAT_HINTS[self.kind]
            field.widget.attrs['placeholder'] = str(FORMAT_HINTS[self.kind])

    def clean_name(self):
        value = self.cleaned_data['name']
        if not self.kind:
            return value.strip()
        canonical = CANONICALIZERS[self.kind](value)
        if canonical is None:
            raise ValidationError(ERROR_MESSAGES[self.kind])
        return canonical


class MultipleFileInput(forms.ClearableFileInput):
    """File picker that accepts any number of files in one selection."""

    allow_multiple_selected = True

    def __init__(self, attrs=None):
        attrs = {'class': 'form-control', **(attrs or {})}
        super().__init__(attrs)


class MultipleFileField(forms.FileField):
    """
    FileField that keeps every selected file instead of only the first one.

    The import view reads the files straight from ``request.FILES.getlist``,
    so the field itself only has to clean each selected file.
    """

    widget = MultipleFileInput

    def clean(self, data, initial=None):
        single_clean = super().clean
        if isinstance(data, (list, tuple)):
            return [single_clean(item, initial) for item in data]
        return single_clean(data, initial)


class ImportFileForm(forms.Form):
    """
    Replaces: filepath = input("[?] Enter path to the data file: ")
    Users can upload one or many files at once, select an entire folder from
    the browser, or list any number of local/server directories or file paths
    for very large datasets.
    """

    file = MultipleFileField(
        label=_("Select data file to import"),
        required=False,
        help_text=_(
            "One record per line: URL, then email or username, then "
            "password, then the IP address, separated by | ; tab or : "
            "-- for example https://example.com/login|name@example.com|"
            "secret|192.168.1.1. Each value must match its own format and "
            "is stored once, however it is written."
        )
    )
    folder_path = forms.CharField(
        label=_("Or enter local/server folder or file paths"),
        max_length=16384,
        required=False,
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'dir': 'ltr',
            'rows': 3,
            'placeholder': _('/path/to/folder'),
        }),
        help_text=_(
            "One path per line -- every text file under each path is read "
            "recursively and its valid lines are stored. Use this for very "
            "large folders already on disk."
        ),
    )
    fast = forms.BooleanField(
        label=_(
            "Maximum speed (drop the search indexes during the import "
            "and rebuild them afterwards)"
        ),
        required=False,
        help_text=_(
            "Recommended for imports of millions of lines: the reference "
            "values are loaded without the trigram search indexes, which "
            "are rebuilt once at the end. Substring (advanced) searches are "
            "slower while the import runs; exact searches are unaffected."
        ),
    )


class SearchForm(forms.Form):
    """
    Replaces: keyword = input("[?] Enter search keyword: ")
    """

    keyword = forms.CharField(
        max_length=200,
        label=_("Search"),
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': _('Domain, username, email, or IP...')
        })
    )


class QuickSearchForm(forms.Form):
    """
    Fast, indexed lookup: search across all fields (domain, URL, email,
    username, password, IP) or target a single field. Matching uses the
    B-tree indexes on the reference tables, returning results across
    millions of rows in well under a second.
    """

    FIELD_CHOICES = [
        ('all', _('Any')),
        ('domain', _('Domain')),
        ('url', _('URL / Domain')),
        ('email', _('Email')),
        ('user', _('Username')),
        ('password', _('Password')),
        ('ip', _('IP address')),
    ]

    field = forms.ChoiceField(
        label=_('Search by'),
        choices=FIELD_CHOICES,
        initial='all',
        required=False,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    q = forms.CharField(
        label=_('Value'),
        max_length=2048,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': _('Enter the exact value to find'),
            'dir': 'ltr',
        }),
    )

    def __init__(self, *args, **kwargs):
        if args and args[0] is not None:
            data = args[0].copy()
            if not data.get('q') and data.get('keyword'):
                data['q'] = data.get('keyword')
            if not data.get('field'):
                data['field'] = 'all'
            args = (data,) + args[1:]
        super().__init__(*args, **kwargs)


class AdvancedSearchForm(forms.Form):
    """
    Per-field advanced queries. Every field is optional and combined with
    AND; an optional free-text keyword is OR-ed across all names.
    """

    SORT_CHOICES = [
        ('newest', _('Newest first')),
        ('oldest', _('Oldest first')),
        ('url', _('URL (A-Z)')),
        ('identifier', _('Identifier (A-Z)')),
    ]

    keyword = forms.CharField(
        max_length=200, required=False, label=_('Keyword'),
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': _('Any of the values above'),
        }),
    )
    url = forms.CharField(
        max_length=2048, required=False, label=_('URL contains'),
        widget=forms.TextInput(attrs={
            'class': 'form-control', 'dir': 'ltr',
            'placeholder': _('example.com'),
        }),
    )
    account_type = forms.ChoiceField(
        required=False, label=_('Account type'),
        choices=[('', _('Any')), ('email', _('Email')), ('user', _('Username'))],
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    email = forms.CharField(
        max_length=255, required=False, label=_('Email contains'),
        widget=forms.TextInput(attrs={
            'class': 'form-control', 'dir': 'ltr',
            'placeholder': _('user@example.com'),
        }),
    )
    username = forms.CharField(
        max_length=255, required=False, label=_('Username contains'),
        widget=forms.TextInput(attrs={
            'class': 'form-control', 'dir': 'auto',
        }),
    )
    password = forms.CharField(
        max_length=500, required=False, label=_('Password contains'),
        widget=forms.TextInput(attrs={
            'class': 'form-control', 'dir': 'auto',
        }),
    )
    ip = forms.CharField(
        max_length=45, required=False, label=_('IP contains'),
        widget=forms.TextInput(attrs={
            'class': 'form-control', 'dir': 'ltr', 'placeholder': _('N/A'),
        }),
    )
    created_from = forms.DateField(
        required=False, label=_('Created from'),
        widget=forms.DateInput(attrs={
            'class': 'form-control', 'type': 'date',
        }),
    )
    created_to = forms.DateField(
        required=False, label=_('Created to'),
        widget=forms.DateInput(attrs={
            'class': 'form-control', 'type': 'date',
        }),
    )
    sort = forms.ChoiceField(
        required=False, label=_('Sort by'),
        choices=SORT_CHOICES,
        initial='newest',
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
