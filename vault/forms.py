"""
Forms for the vault.

They replace the manual ``input()`` prompts of the original CLI script. The
record form is the interesting one: it takes the *names* of the pieces of a
login (URL, email, username, password, IP) and resolves each through the
reference tables on save, so a value that already exists is reused instead of
being typed a second time.
"""

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from .models import Account, Email, Ip, Pass, Url, User
from .utils import extract_domain


class AccountForm(forms.ModelForm):
    """
    Captures a "login" (Account) by its component names instead of raw
    foreign keys. On save each component is resolved with get_or_create,
    so an existing URL / email / username / password / IP is reused
    instead of being entered a second time.
    """

    url_name = forms.CharField(
        label=_('URL'),
        max_length=2048,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'dir': 'ltr',
            'list': 'url-list',
            'placeholder': 'https://example.com/login',
            'autocomplete': 'off',
        }),
    )
    email_name = forms.EmailField(
        label=_('Email'),
        required=False,
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'dir': 'ltr',
            'list': 'email-list',
            'placeholder': _('user@example.com'),
            'autocomplete': 'off',
        }),
    )
    user_name = forms.CharField(
        label=_('Username'),
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'dir': 'auto',
            'list': 'user-list',
            'placeholder': _('username'),
            'autocomplete': 'off',
        }),
    )
    password_name = forms.CharField(
        label=_('Password'),
        max_length=500,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'dir': 'auto',
            'placeholder': _('Password or secret'),
            'autocomplete': 'new-password',
        }),
    )
    ip_name = forms.CharField(
        label=_('IP address'),
        max_length=45,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'dir': 'ltr',
            'list': 'ip-list',
            'placeholder': _('N/A'),
            'autocomplete': 'off',
        }),
    )

    class Meta:
        model = Account
        fields = ['account_type']
        widgets = {
            'account_type': forms.Select(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The account type drives which identifier field is shown, so it goes
        # first even though it comes from the model.
        self.order_fields([
            'account_type', 'url_name', 'email_name', 'user_name',
            'password_name', 'ip_name',
        ])
        instance = self.instance
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
        cleaned = super().clean()
        account_type = cleaned.get('account_type')
        email_name = (cleaned.get('email_name') or '').strip()
        user_name = (cleaned.get('user_name') or '').strip()

        if account_type == 'email':
            if not email_name:
                self.add_error(
                    'email_name',
                    _('An email address is required for email accounts.'),
                )
            if user_name:
                self.add_error(
                    'user_name',
                    _('Email accounts cannot have a username.'),
                )
        else:
            if not user_name:
                self.add_error(
                    'user_name',
                    _('A username is required for user accounts.'),
                )
            if email_name:
                self.add_error(
                    'email_name',
                    _('User accounts cannot have an email.'),
                )
        return cleaned

    def save(self, commit=True):
        cleaned = self.cleaned_data
        account_type = cleaned['account_type']

        url_name = cleaned['url_name'].strip()
        ip_name = (cleaned.get('ip_name') or '').strip()
        secret = cleaned['password_name']

        url, _created = Url.objects.get_or_create(
            name=url_name, defaults={'host': extract_domain(url_name)}
        )
        password, _created = Pass.objects.get_or_create(name=secret)

        ip = None
        if ip_name and ip_name.upper() != 'N/A':
            ip, _created = Ip.objects.get_or_create(name=ip_name)

        email = None
        user = None
        if account_type == 'email':
            email, _created = Email.objects.get_or_create(
                name=cleaned['email_name'].strip()
            )
        else:
            user, _created = User.objects.get_or_create(
                name=cleaned['user_name'].strip()
            )

        self.instance.url = url
        self.instance.account_type = account_type
        self.instance.email = email
        self.instance.user = user
        self.instance.password = password
        self.instance.ip = ip

        # Duplicate guard: mirror the database UNIQUE index so the user gets
        # a friendly form error instead of a 500.
        if Account.duplicate_queryset(
            url=url.pk, account_type=account_type, password=password.pk,
            ip=ip.pk if ip else None,
            email=email.pk if email else None,
            user=user.pk if user else None,
            exclude_pk=self.instance.pk,
        ).exists():
            raise ValidationError(_('This exact relationship already exists.'))

        return super().save(commit=commit)


class ReferenceForm(forms.Form):
    """Add a single value to one of the reference tables."""

    MAX_LENGTHS = {'url': 2048, 'email': 255, 'user': 255, 'pass': 500,
                   'ip': 45}

    name = forms.CharField(
        label=_('Name'),
        max_length=2048,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'dir': 'auto',
            'placeholder': _('Enter a value'),
        }),
    )

    def __init__(self, *args, kind='url', label=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = kind
        self.label = label or kind
        self.fields['name'].max_length = self.MAX_LENGTHS.get(kind, 2048)
        if kind in ('url', 'email', 'ip'):
            self.fields['name'].widget.attrs['dir'] = 'ltr'
        if kind == 'pass':
            self.fields['name'].widget.attrs['dir'] = 'auto'

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        if self.kind == 'ip' and not all(
            c in '0123456789.:%abcdefABCDEF-' for c in name
        ):
            raise ValidationError(_('This does not look like an IP address.'))
        return name


class MultipleFileInput(forms.FileInput):
    """
    Django's default file widget refuses to receive more than one file. The
    bulk importer needs exactly that: a folder pick arrives as one field name
    with many files behind it.
    """

    allow_multiple_selected = True


class ImportFileForm(forms.Form):
    """
    Replaces: ``filepath = input("[?] Enter path to the data file: ")``

    One file, a whole folder picked in the browser, or a folder that already
    lives on the server -- all three end up in the same streaming importer.

    The two upload fields are plain ``Field``s on purpose: their value is a
    *list* of uploads, which ``FileField`` would reject, and the view reads the
    files straight from ``request.FILES`` anyway.
    """

    file = forms.Field(
        label=_("Select data files to import"),
        required=False,
        help_text=_(
            "Supported formats: TXT with colon, pipe, tab, or semicolon "
            "delimiters"
        ),
        widget=MultipleFileInput(attrs={'class': 'form-control'}),
    )
    folder = forms.Field(
        label=_("Or select a whole folder"),
        required=False,
        help_text=_(
            "Pick a folder and every text file inside it (including "
            "subfolders) will be imported. For very large volumes, prefer "
            "the importfolder management command."
        ),
        widget=MultipleFileInput(attrs={
            'class': 'form-control',
            'webkitdirectory': True,
            'directory': True,
            'mozdirectory': True,
        }),
    )
    server_path = forms.CharField(
        label=_("Or a folder already on the server"),
        required=False,
        max_length=1024,
        help_text=_(
            "Absolute path to a directory (or a single file) the server can "
            "read. This is the fastest way to ingest millions of lines "
            "because nothing is uploaded."
        ),
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'dir': 'ltr',
            'placeholder': '/var/log/vault-imports',
        }),
    )

    def __init__(self, *args, uploads=None, **kwargs):
        super().__init__(*args, **kwargs)
        # A browser folder picker sends many files under one field name, so
        # the view hands over the full list rather than the single value a
        # plain FileField would resolve to.
        self.uploads = list(uploads or [])

    def clean_server_path(self):
        return (self.cleaned_data.get('server_path') or '').strip()

    def clean(self):
        cleaned = super().clean()
        if not self.uploads and not (cleaned.get('server_path') or '').strip():
            raise ValidationError(
                _('Please choose a file, a folder, or a server path.')
            )
        return cleaned


class QuickSearchForm(forms.Form):
    """
    Fast, indexed lookup: pick the field (domain, URL, email, username,
    password, IP) and enter the exact value. The lookup runs against a unique
    index, so it returns every matching record in well under a second even
    across millions of rows.
    """

    FIELD_CHOICES = [
        ('domain', _('Domain')),
        ('url', _('Full URL')),
        ('email', _('Email')),
        ('user', _('Username')),
        ('password', _('Password')),
        ('ip', _('IP address')),
    ]

    field = forms.ChoiceField(
        label=_('Search by'),
        choices=FIELD_CHOICES,
        initial='domain',
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    q = forms.CharField(
        label=_('Value'),
        max_length=2048,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': _('Enter the exact value to find'),
            'dir': 'auto',
        }),
    )


class AdvancedSearchForm(forms.Form):
    """
    Per-field advanced queries. Every field is optional and combined with
    AND; an optional free-text keyword is OR-ed across all names. This is the
    flexible-but-slower path -- it scans with LIKE, so it is not the one to
    use to find something in millions of rows quickly.
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
    domain = forms.CharField(
        max_length=255, required=False, label=_('Domain contains'),
        widget=forms.TextInput(attrs={
            'class': 'form-control', 'dir': 'ltr',
            'placeholder': _('example.com'),
        }),
    )
    url = forms.CharField(
        max_length=2048, required=False, label=_('URL contains'),
        widget=forms.TextInput(attrs={
            'class': 'form-control', 'dir': 'ltr',
        }),
    )
    account_type = forms.ChoiceField(
        required=False, label=_('Account type'),
        choices=[('', _('Any')), ('email', _('Email')),
                 ('user', _('Username'))],
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
