from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from .models import (
    Account, Email, Ip, Pass, Url, User,
)


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
        widget=forms.URLInput(attrs={
            'class': 'form-control',
            'dir': 'ltr',
            'list': 'url-list',
            'placeholder': _('https://example.com/login'),
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
        }),
    )
    password_name = forms.CharField(
        label=_('Password'),
        max_length=500,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'dir': 'auto',
            'placeholder': _('Password or secret'),
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

        url, _created = Url.objects.get_or_create(
            name=cleaned['url_name'].strip()
        )
        password, _created = Pass.objects.get_or_create(
            name=cleaned['password_name']
        )

        ip = None
        ip_name = (cleaned.get('ip_name') or '').strip()
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

        # Duplicate guard (mirrors Account.save for a friendly form error).
        if url and password:
            qs = Account.objects.filter(
                url=url, account_type=account_type,
                password=password, ip=ip,
            )
            if account_type == 'email':
                qs = qs.filter(email=email, user__isnull=True)
            else:
                qs = qs.filter(user=user, email__isnull=True)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise ValidationError(
                    _('This exact relationship already exists.')
                )

        return super().save(commit=commit)


class ReferenceForm(forms.Form):
    """Add a single value to one of the reference tables."""

    name = forms.CharField(
        label=_('Name'),
        max_length=2048,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': _('Enter a value'),
        }),
    )


class ImportFileForm(forms.Form):
    """
    Replaces: filepath = input("[?] Enter path to the data file: ")
    Users can upload a file, select an entire folder from the browser, or
    provide a local/server directory or file path for very large datasets.
    """

    file = forms.FileField(
        label=_("Select data file to import"),
        required=False,
        help_text=_(
            "Supported formats: TXT with colon, pipe, tab, or semicolon "
            "delimiters"
        )
    )
    folder_path = forms.CharField(
        label=_("Or enter a local/server folder or file path"),
        max_length=2048,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'dir': 'ltr',
            'placeholder': _('/path/to/folder'),
        }),
        help_text=_(
            "For very large folders already on disk, enter the directory "
            "path here to recursively read every file and store valid lines."
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
