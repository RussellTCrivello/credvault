from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _


class Url(models.Model):
    """
    URL reference table.

    Exactly three fields, as requested:
        * Name of the URL
        * Construction date
        * Date of adjustment
    """

    name = models.CharField(_('Name of the URL'), max_length=2048, unique=True)
    created_at = models.DateTimeField(_('Construction date'), auto_now_add=True)
    updated_at = models.DateTimeField(_('Date of adjustment'), auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = _('URL')
        verbose_name_plural = _('URLs')
        indexes = [
            models.Index(Lower('name'), name='vault_url_name_lower_idx'),
        ]

    def __str__(self):
        return self.name


class Email(models.Model):
    """Email reference table (name, construction date, adjustment date)."""

    name = models.CharField(_('Name of the email'), max_length=255, unique=True)
    created_at = models.DateTimeField(_('Construction date'), auto_now_add=True)
    updated_at = models.DateTimeField(_('Date of adjustment'), auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = _('Email')
        verbose_name_plural = _('Emails')
        indexes = [
            models.Index(Lower('name'), name='vault_email_name_lower_idx'),
        ]

    def __str__(self):
        return self.name


class User(models.Model):
    """Username reference table (name, construction date, adjustment date)."""

    name = models.CharField(_('Name of the user'), max_length=255, unique=True)
    created_at = models.DateTimeField(_('Construction date'), auto_now_add=True)
    updated_at = models.DateTimeField(_('Date of adjustment'), auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = _('Username')
        verbose_name_plural = _('Usernames')
        indexes = [
            models.Index(Lower('name'), name='vault_user_name_lower_idx'),
        ]

    def __str__(self):
        return self.name


class Pass(models.Model):
    """Password reference table (name, construction date, adjustment date)."""

    name = models.CharField(_('Name of the pass'), max_length=500, unique=True)
    created_at = models.DateTimeField(_('Construction date'), auto_now_add=True)
    updated_at = models.DateTimeField(_('Date of adjustment'), auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = _('Password')
        verbose_name_plural = _('Passwords')
        indexes = [
            models.Index(Lower('name'), name='vault_pass_name_lower_idx'),
        ]

    def __str__(self):
        return self.name


class Ip(models.Model):
    """IP address reference table (name, construction date, adjustment date)."""

    name = models.CharField(_('Name of the IP'), max_length=45, unique=True)
    created_at = models.DateTimeField(_('Construction date'), auto_now_add=True)
    updated_at = models.DateTimeField(_('Date of adjustment'), auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = _('IP address')
        verbose_name_plural = _('IP addresses')
        indexes = [
            models.Index(Lower('name'), name='vault_ip_name_lower_idx'),
        ]

    def __str__(self):
        return self.name


class Account(models.Model):
    """
    The relationship that ties the reference tables together.

    A single "login" record links one URL, one account identifier
    (an Email *or* a Username, chosen by account_type), one Password
    and one IP address. Because those values live in their own tables,
    they are never entered twice -- the same URL / email / username /
    password / IP is reused through foreign keys.

    This is the entity the rest of the UI calls a "Record".
    """

    ACCOUNT_TYPE_CHOICES = [
        ('email', _('Email')),
        ('user', _('Username')),
    ]

    url = models.ForeignKey(
        Url, on_delete=models.PROTECT, related_name='accounts',
        verbose_name=_('URL'),
    )
    account_type = models.CharField(
        _('Account type'), max_length=10,
        choices=ACCOUNT_TYPE_CHOICES, default='user',
    )
    email = models.ForeignKey(
        Email, on_delete=models.PROTECT, null=True, blank=True,
        related_name='accounts', verbose_name=_('Email'),
    )
    user = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True,
        related_name='accounts', verbose_name=_('Username'),
    )
    password = models.ForeignKey(
        Pass, on_delete=models.PROTECT, related_name='accounts',
        verbose_name=_('Password'),
    )
    ip = models.ForeignKey(
        Ip, on_delete=models.PROTECT, null=True, blank=True,
        related_name='accounts', verbose_name=_('IP address'),
    )
    created_at = models.DateTimeField(_('Construction date'), auto_now_add=True)
    updated_at = models.DateTimeField(_('Date of adjustment'), auto_now=True)

    class Meta:
        ordering = ['-created_at', 'id']
        verbose_name = _('Record')
        verbose_name_plural = _('Records')
        indexes = [
            models.Index(
                fields=['-created_at', 'id'],
                name='vault_acct_created_id_idx',
            ),
        ]

    def __str__(self):
        return f"{self.url.name} - {self.identifier}"

    @property
    def identifier(self):
        if self.account_type == 'email' and self.email:
            return self.email.name
        if self.user:
            return self.user.name
        return '—'

    def save(self, *args, **kwargs):
        # Guard against creating the exact same relationship twice.
        if self.url_id and self.password_id:
            qs = Account.objects.filter(
                url=self.url,
                account_type=self.account_type,
                password=self.password,
                ip=self.ip,
            )
            if self.account_type == 'email':
                qs = qs.filter(email=self.email, user__isnull=True)
            else:
                qs = qs.filter(user=self.user, email__isnull=True)
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError(
                    _('This exact relationship already exists.')
                )
        super().save(*args, **kwargs)
