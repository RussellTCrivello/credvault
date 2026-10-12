import hashlib

from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, router, transaction
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from . import lookups  # noqa: F401  (registers contains_ci on the text fields)
from .validators import (
    CANONICALIZERS, ERROR_MESSAGES, MAX_EMAIL_LENGTH, MAX_IP_LENGTH,
    MAX_PASSWORD_LENGTH, MAX_URL_LENGTH, MAX_USERNAME_LENGTH,
)


def record_fingerprint(
    url_id, account_type, email_id, user_id, password_id, ip_id
):
    """
    Stable hash of the six columns that identify a login relationship.

    Storing it in a unique column makes "never store the same record twice"
    a guarantee enforced by the database rather than a check that a write
    path could forget -- including the bulk import path, which builds rows
    without calling ``save()``.
    """
    payload = (
        f'{url_id or ""}|{account_type or ""}|{email_id or ""}'
        f'|{user_id or ""}|{password_id or ""}|{ip_id or ""}'
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


class CanonicalNameModel(models.Model):
    """
    Shared behaviour of the five reference tables.

    A value is canonicalized on the way in by both ``clean()`` (used by the
    admin and by forms) and ``save()``, so no write path can store a second
    spelling of a value that is already present.  Together with
    ``unique=True`` on ``name`` this is what keeps a URL, email, username,
    password or IP from ever being entered twice:

        'HTTPS://Example.COM/login'  ->  'https://example.com/login'
        ' Person@Example.COM '       ->  'person@example.com'
        '192.168.001.001'            ->  rejected (leading zeros)
        '2001:0DB8::0001'            ->  '2001:db8::1'

    See vault.validators for the exact format of each kind.
    """

    #: Reference kind: 'url', 'email', 'user', 'pass' or 'ip'.
    KIND = None

    class Meta:
        abstract = True

    @classmethod
    def canonicalize(cls, value):
        """Canonical spelling of `value`, or None when the format is wrong."""
        return CANONICALIZERS[cls.KIND](value)

    @classmethod
    def require_canonical(cls, value):
        """Canonical spelling of `value`, raising when the format is wrong."""
        canonical = cls.canonicalize(value)
        if canonical is None:
            raise ValidationError({'name': ValidationError(
                ERROR_MESSAGES[cls.KIND], code=f'invalid_{cls.KIND}'
            )})
        return canonical

    def clean(self):
        super().clean()
        self.name = self.require_canonical(self.name)

    def save(self, *args, **kwargs):
        self.name = self.require_canonical(self.name)
        already_stored = type(self).objects.filter(name=self.name)
        if self.pk:
            already_stored = already_stored.exclude(pk=self.pk)
        if already_stored.exists():
            raise _duplicate_error(self)
        # Two requests can pass the check above at the same time; the unique
        # index then rejects the second insert. The savepoint keeps that
        # failure from breaking an enclosing transaction.
        try:
            with transaction.atomic(using=router.db_for_write(type(self), instance=self)):
                super().save(*args, **kwargs)
        except IntegrityError:
            raise _duplicate_error(self)


def _duplicate_error(instance):
    return ValidationError({'name': ValidationError(
        _('This %(kind)s is already stored.')
        % {'kind': instance._meta.verbose_name},
        code='duplicate_value',
    )})


class Url(CanonicalNameModel):
    """
    URL reference table.

    Exactly three fields, as requested:
        * Name of the URL
        * Construction date
        * Date of adjustment
    """

    KIND = 'url'

    name = models.CharField(_('Name of the URL'), max_length=MAX_URL_LENGTH, unique=True)
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


class Email(CanonicalNameModel):
    """Email reference table (name, construction date, adjustment date)."""

    KIND = 'email'

    name = models.CharField(_('Name of the email'), max_length=MAX_EMAIL_LENGTH, unique=True)
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


class User(CanonicalNameModel):
    """Username reference table (name, construction date, adjustment date)."""

    KIND = 'user'

    name = models.CharField(_('Name of the user'), max_length=MAX_USERNAME_LENGTH, unique=True)
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


class Pass(CanonicalNameModel):
    """Password reference table (name, construction date, adjustment date)."""

    KIND = 'pass'

    name = models.CharField(_('Name of the pass'), max_length=MAX_PASSWORD_LENGTH, unique=True)
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


class Ip(CanonicalNameModel):
    """IP address reference table (name, construction date, adjustment date)."""

    KIND = 'ip'

    name = models.CharField(_('Name of the IP'), max_length=MAX_IP_LENGTH, unique=True)
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

    The whole combination is hashed into `fingerprint`, which is unique, so
    the same record cannot be stored twice either.

    This is the entity the rest of the UI calls a "Record".

    Indexing: every foreign key is indexed together with the record id, as
    ``(fk, -id)``. That one index answers both "records that use this value"
    and "the newest records that use this value" by reading the index
    backwards, so a search for one password returns its first page without
    sorting every record that shares it -- which is what keeps lookups fast
    when a value is used by millions of records.
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
    fingerprint = models.CharField(
        _('Record fingerprint'), max_length=64, unique=True, editable=False,
    )
    created_at = models.DateTimeField(_('Construction date'), auto_now_add=True)
    updated_at = models.DateTimeField(_('Date of adjustment'), auto_now=True)

    class Meta:
        # Record ids grow with every insert, so the newest record is the one
        # with the highest id. Sorting by id lets the (fk, -id) indexes return
        # results already in order.
        ordering = ['-id']
        verbose_name = _('Record')
        verbose_name_plural = _('Records')
        indexes = [
            models.Index(fields=['url', '-id'], name='vault_acct_url_idx'),
            models.Index(fields=['email', '-id'], name='vault_acct_email_idx'),
            models.Index(fields=['user', '-id'], name='vault_acct_user_idx'),
            models.Index(fields=['password', '-id'], name='vault_acct_pass_idx'),
            models.Index(fields=['ip', '-id'], name='vault_acct_ip_idx'),
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

    def compute_fingerprint(self):
        return record_fingerprint(
            self.url_id, self.account_type, self.email_id,
            self.user_id, self.password_id, self.ip_id,
        )

    def save(self, *args, **kwargs):
        self.fingerprint = self.compute_fingerprint()
        duplicates = Account.objects.filter(fingerprint=self.fingerprint)
        if self.pk:
            duplicates = duplicates.exclude(pk=self.pk)
        if duplicates.exists():
            raise ValidationError(
                _('This exact relationship already exists.')
            )
        try:
            with transaction.atomic(using=router.db_for_write(Account, instance=self)):
                super().save(*args, **kwargs)
        except IntegrityError:
            raise ValidationError(
                _('This exact relationship already exists.')
            )


class PerformanceSettings(models.Model):
    """
    The workspace speed profile chosen on the Settings page (a single row).

    ``import_mode`` decides how records are written during an import and
    ``query_mode`` decides how many results and matches a search reads and
    counts. The exact numbers behind each mode live in ``vault.performance``.
    """

    MODE_CHOICES = [
        ('balanced', _('Balanced')),
        ('fast', _('Fast')),
        ('maximum', _('Maximum speed')),
    ]
    COMPUTE_CHOICES = [
        ('cpu', _('CPU')),
        ('gpu', _('GPU (NVIDIA)')),
    ]

    import_mode = models.CharField(
        _('Import speed'), max_length=10, choices=MODE_CHOICES,
        default='balanced',
    )
    query_mode = models.CharField(
        _('Query speed'), max_length=10, choices=MODE_CHOICES,
        default='balanced',
    )
    # Recorded for a future GPU step. Nothing uses it yet: all work runs on the CPU.
    compute_mode = models.CharField(
        _('Compute'), max_length=5, choices=COMPUTE_CHOICES, default='cpu',
    )
    updated_at = models.DateTimeField(_('Date of adjustment'), auto_now=True)

    class Meta:
        verbose_name = _('Performance settings')
        verbose_name_plural = _('Performance settings')

    @classmethod
    def load(cls):
        """Return the single settings row, creating it with defaults once."""
        settings_row, _created = cls.objects.get_or_create(pk=1)
        return settings_row

    def __str__(self):
        return str(self._meta.verbose_name)
