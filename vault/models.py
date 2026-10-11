
from django.db import models
from django.utils.translation import gettext_lazy as _


class Record(models.Model):
    """
    Replaces the raw SQL table 'records'.
    
    Original SQL:
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT NOT NULL,
            url TEXT NOT NULL,
            account_type TEXT NOT NULL,
            account_identifier TEXT NOT NULL,
            secret_data TEXT NOT NULL,
            ip_address TEXT DEFAULT 'N/A'
        )
    
    Django auto-creates 'id' as an auto-incrementing primary key.
    """

    ACCOUNT_TYPE_CHOICES = [
        ('email', _('Email')),
        ('user', _('Username')),
    ]

    domain = models.CharField(_('Domain'), max_length=255)
    url = models.URLField(_('Full URL'), max_length=2048)
    account_type = models.CharField(
        _('Account type'),
        max_length=10,
        choices=ACCOUNT_TYPE_CHOICES,
        default='user'
    )
    account_identifier = models.CharField(_('Account identifier'), max_length=255)
    secret_data = models.CharField(_('Secret data'), max_length=500)
    ip_address = models.CharField(_('IP address'), max_length=45, default='N/A')

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"{self.domain} - {self.account_identifier}"