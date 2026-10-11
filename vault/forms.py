
from django import forms
from django.utils.translation import gettext_lazy as _
from .models import Record


class RecordForm(forms.ModelForm):
    """
    Replaces the manual input() prompts in add_record() and update_record().
    Django ModelForm auto-generates form fields from the model.
    """

    class Meta:
        model = Record
        fields = [
            'domain', 'url', 'account_type',
            'account_identifier', 'secret_data', 'ip_address'
        ]
        widgets = {
            'domain': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': _('example.com')
            }),
            'url': forms.URLInput(attrs={
                'class': 'form-control',
                'dir': 'ltr',
                'placeholder': _('https://example.com/login')
            }),
            'account_type': forms.Select(attrs={
                'class': 'form-control'
            }),
            'account_identifier': forms.TextInput(attrs={
                'class': 'form-control',
                'dir': 'auto',
                'placeholder': _('user@example.com')
            }),
            'secret_data': forms.TextInput(attrs={
                'class': 'form-control',
                'dir': 'auto',
                'placeholder': _('Password or secret')
            }),
            'ip_address': forms.TextInput(attrs={
                'class': 'form-control',
                'dir': 'ltr',
                'placeholder': _('N/A')
            }),
        }


class ImportFileForm(forms.Form):
    """
    Replaces: filepath = input("[?] Enter path to the data file: ")
    Instead of typing a path, users upload a file through the browser.
    """
    file = forms.FileField(
        label=_("Select data file to import"),
        help_text=_("Supported formats: TXT with colon, pipe, tab, or semicolon delimiters")
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