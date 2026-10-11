"""
Normalized data model for the vault.

Five *reference tables* hold the shared values (URL, email, username,
password, IP). Each value is stored exactly once and is reused through
foreign keys, so nothing is ever entered twice. ``Account`` is the
relationship that ties them together and is what the interface calls a
"record".

The layout is chosen around two requirements:

* Ingesting very large volumes -- millions of lines -- through bulk,
  set-based inserts (see :mod:`vault.importer`).
* Answering "give me every record for this exact URL / domain / email /
  username / password / IP" in well under a second. Every lookup goes
  through a unique index on the reference table and an index on the
  foreign key, so the query cost follows the size of the *result*, not the
  size of the table.
"""

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from .utils import extract_domain


class ReferenceValue(models.Model):
    """
    Shared shape of the five reference tables.

    Exactly three fields, as requested: the value itself plus the two
    dates. Case-insensitive lookups are served by an expression index on
    ``LOWER(name)``, which keeps the tables searchable at scale without
    adding a fourth column.
    """

    name = models.CharField(_('Name'), max_length=255, unique=True)
    created_at = models.DateTimeField(_('Construction date'), auto_now_add=True)
    updated_at = models.DateTimeField(_('Date of adjustment'), auto_now=True)

    class Meta:
        abstract = True
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def key(self):
        """Lowercased value, the form used by case-insensitive lookups."""
        return (self.name or '').strip().lower()


class Url(ReferenceValue):
    """
    URL reference table.

    ``host`` is not a user-editable field: it is derived from ``name`` when
    the row is written so that a "domain" search is an equality lookup on an
    indexed column instead of a ``LIKE '%domain%'`` scan of every row.
    """

    name = models.CharField(_('Name of the URL'), max_length=2048, unique=True)
    host = models.CharField(
        _('Host'), max_length=255, blank=True, editable=False, default='',
        db_index=True,
    )

    class Meta(ReferenceValue.Meta):
        verbose_name = _('URL')
        verbose_name_plural = _('URLs')
        indexes = [
            models.Index(Lower('name'), name='%(app_label)s_%(class)s_lower'),
        ]

    def save(self, *args, **kwargs):
        if 'update_fields' not in kwargs:
            self.host = extract_domain(self.name)[:255]
        elif 'host' not in kwargs['update_fields']:
            kwargs = dict(kwargs)
            kwargs['update_fields'] = [*kwargs['update_fields'], 'host']
            self.host = extract_domain(self.name)[:255]
        super().save(*args, **kwargs)


class Email(ReferenceValue):
    """Email reference table (name, construction date, adjustment date)."""

    name = models.CharField(_('Name of the email'), max_length=255, unique=True)

    class Meta(ReferenceValue.Meta):
        verbose_name = _('Email')
        verbose_name_plural = _('Emails')
        indexes = [
            models.Index(Lower('name'), name='%(app_label)s_%(class)s_lower'),
        ]


class User(ReferenceValue):
    """Username reference table (name, construction date, adjustment date)."""

    name = models.CharField(_('Name of the user'), max_length=255, unique=True)

    class Meta(ReferenceValue.Meta):
        verbose_name = _('Username')
        verbose_name_plural = _('Usernames')
        indexes = [
            models.Index(Lower('name'), name='%(app_label)s_%(class)s_lower'),
        ]


class Pass(ReferenceValue):
    """
    Password reference table (name, construction date, adjustment date).

    Uniqueness here is deliberately case-*sensitive* -- two secrets that
    differ only in capitalization are two different secrets -- while search
    stays case-insensitive through the expression index.
    """

    name = models.CharField(_('Name of the pass'), max_length=500, unique=True)

    class Meta(ReferenceValue.Meta):
        verbose_name = _('Password')
        verbose_name_plural = _('Passwords')
        indexes = [
            models.Index(Lower('name'), name='%(app_label)s_%(class)s_lower'),
        ]


class Ip(ReferenceValue):
    """IP address reference table (name, construction date, adjustment date)."""

    name = models.CharField(_('Name of the IP'), max_length=45, unique=True)

    class Meta(ReferenceValue.Meta):
        verbose_name = _('IP address')
        verbose_name_plural = _('IP addresses')
        indexes = [
            models.Index(Lower('name'), name='%(app_label)s_%(class)s_lower'),
        ]


class Account(models.Model):
    """
    The relationship that ties the reference tables together.

    A single "login" links one URL, one account identifier (an Email *or* a
    Username, chosen by ``account_type``), one Password and one optional IP
    address. Because the values live in their own tables they are never
    entered twice -- the same URL / email / username / password / IP is
    reused through foreign keys.

    The identical relationship can never be stored twice: a UNIQUE index over
    the (nullable-aware) foreign-key combination is enforced by the database,
    which is what makes an idempotent, resumable bulk import possible without
    reading the existing table into memory.
    """

    ACCOUNT_TYPE_CHOICES = [
        ('email', _('Email')),
        ('user', _('Username')),
    ]

    url = models.ForeignKey(
        Url, on_delete=models.PROTECT, related_name='accounts',
        verbose_name=_('URL'), db_index=False,
    )
    account_type = models.CharField(
        _('Account type'), max_length=10,
        choices=ACCOUNT_TYPE_CHOICES, default='user',
    )
    email = models.ForeignKey(
        Email, on_delete=models.PROTECT, null=True, blank=True,
        related_name='accounts', verbose_name=_('Email'), db_index=False,
    )
    user = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True,
        related_name='accounts', verbose_name=_('Username'), db_index=False,
    )
    password = models.ForeignKey(
        Pass, on_delete=models.PROTECT, related_name='accounts',
        verbose_name=_('Password'), db_index=False,
    )
    ip = models.ForeignKey(
        Ip, on_delete=models.PROTECT, null=True, blank=True,
        related_name='accounts', verbose_name=_('IP address'), db_index=False,
    )
    created_at = models.DateTimeField(_('Construction date'), auto_now_add=True)
    updated_at = models.DateTimeField(_('Date of adjustment'), auto_now=True)

    class Meta:
        ordering = ['-created_at', '-id']
        verbose_name = _('Record')
        verbose_name_plural = _('Records')
        # Each search path gets one index that serves both the equality
        # lookup and the newest-first ordering, so no sort is needed.
        indexes = [
            models.Index(fields=['url', '-created_at', '-id'],
                         name='account_by_url'),
            models.Index(fields=['email', '-created_at', '-id'],
                         name='account_by_email'),
            models.Index(fields=['user', '-created_at', '-id'],
                         name='account_by_user'),
            models.Index(fields=['password', '-created_at', '-id'],
                         name='account_by_password'),
            models.Index(fields=['ip', '-created_at', '-id'],
                         name='account_by_ip'),
            models.Index(fields=['-created_at', '-id'],
                         name='account_recent'),
        ]

    def __str__(self):
        return f"{self.url.name} - {self.identifier}"

    @property
    def identifier(self):
        """The email or username this record logs in with."""
        if self.account_type == 'email' and self.email_id:
            return self.email.name if self.email else '—'
        if self.user_id:
            return self.user.name if self.user else '—'
        return '—'

    @classmethod
    def duplicate_queryset(cls, *, url, account_type, password, ip,
                           email=None, user=None, exclude_pk=None):
        """Every stored record holding this exact relationship."""
        qs = cls.objects.filter(
            url_id=url,
            account_type=account_type,
            password_id=password,
            ip_id=ip,
        )
        if account_type == 'email':
            qs = qs.filter(email_id=email, user__isnull=True)
        else:
            qs = qs.filter(user_id=user, email__isnull=True)
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        return qs

    def relationship_key(self):
        """The tuple the database UNIQUE index is built on."""
        return (
            self.url_id, self.account_type,
            self.email_id or 0, self.user_id or 0,
            self.password_id, self.ip_id or 0,
        )

    def _is_duplicate(self):
        """True when this exact relationship is already stored."""
        if not (self.url_id and self.password_id):
            return False
        return Account.duplicate_queryset(
            url=self.url_id, account_type=self.account_type,
            password=self.password_id, ip=self.ip_id,
            email=self.email_id, user=self.user_id,
            exclude_pk=self.pk,
        ).exists()

    def clean(self):
        super().clean()
        if self._is_duplicate():
            raise ValidationError(
                _('This exact relationship already exists.')
            )

    def save(self, *args, **kwargs):
        # Friendly error for the single-record forms; the bulk importer
        # bypasses save() and relies on the UNIQUE index instead.
        if self._is_duplicate():
            raise ValidationError(_('This exact relationship already exists.'))
        super().save(*args, **kwargs)
