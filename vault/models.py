import hashlib
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

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
            raise ValidationError({'name': ValidationError(
                _('This %(kind)s is already stored.')
                % {'kind': self._meta.verbose_name},
                code='duplicate_value',
            )})
        super().save(*args, **kwargs)


class Url(CanonicalNameModel):
    """
    URL reference table.

    Exactly three fields, as requested:
        * Name of the URL
        * Created at
        * Updated at
    """

    KIND = 'url'

    name = models.CharField(_('Name of the URL'), max_length=MAX_URL_LENGTH, unique=True)
    created_at = models.DateTimeField(_('Created at'), auto_now_add=True)
    updated_at = models.DateTimeField(_('Updated at'), auto_now=True)

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
    """Email reference table (name, created at, updated at)."""

    KIND = 'email'

    name = models.CharField(_('Name of the email'), max_length=MAX_EMAIL_LENGTH, unique=True)
    created_at = models.DateTimeField(_('Created at'), auto_now_add=True)
    updated_at = models.DateTimeField(_('Updated at'), auto_now=True)

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
    """Username reference table (name, created at, updated at)."""

    KIND = 'user'

    name = models.CharField(_('Name of the user'), max_length=MAX_USERNAME_LENGTH, unique=True)
    created_at = models.DateTimeField(_('Created at'), auto_now_add=True)
    updated_at = models.DateTimeField(_('Updated at'), auto_now=True)

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
    """Password reference table (name, created at, updated at)."""

    KIND = 'pass'

    name = models.CharField(_('Name of the pass'), max_length=MAX_PASSWORD_LENGTH, unique=True)
    created_at = models.DateTimeField(_('Created at'), auto_now_add=True)
    updated_at = models.DateTimeField(_('Updated at'), auto_now=True)

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
    """IP address reference table (name, created at, updated at)."""

    KIND = 'ip'

    name = models.CharField(_('Name of the IP'), max_length=MAX_IP_LENGTH, unique=True)
    created_at = models.DateTimeField(_('Created at'), auto_now_add=True)
    updated_at = models.DateTimeField(_('Updated at'), auto_now=True)

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
    fingerprint = models.CharField(
        _('Record fingerprint'), max_length=64, unique=True, editable=False,
    )
    created_at = models.DateTimeField(_('Created at'), auto_now_add=True)
    updated_at = models.DateTimeField(_('Updated at'), auto_now=True)

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
        super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# Import job tracking and record-level ledger
# ---------------------------------------------------------------------------
# These models provide durable import tracking so that every import is
# traceable, interruption-safe, resumable, idempotent, and verifiable.
# See the IMPORT_LIFECYCLE design document for the state machine and
# recovery protocol.

#: How long a worker lease lasts before it expires and another worker
#: can take over.  Default: 5 minutes.
IMPORT_LEASE_SECONDS = 300


class ImportJob(models.Model):
    """
    Persistent record of one import operation.

    Survives application restarts, process crashes and forced termination.
    The authoritative source of truth for import progress -- the frontend
    must never be the only place progress is tracked.

    State machine:
        pending -> processing -> completed
                              -> completed_with_errors
                              -> failed
        processing -> paused -> processing  (user resumes)
        processing -> cancelled
        processing -> interrupted  (crash / kill -- recovered on restart)
        interrupted -> recovering -> processing  (recovery run)
    """

    STATUS_CHOICES = [
        ('pending', _('Pending')),
        ('processing', _('Processing')),
        ('paused', _('Paused')),
        ('completed', _('Completed')),
        ('completed_with_errors', _('Completed with errors')),
        ('failed', _('Failed')),
        ('cancelled', _('Cancelled')),
        ('interrupted', _('Interrupted')),
    ]

    STAGE_CHOICES = [
        ('initializing', _('Initializing')),
        ('scanning', _('Scanning')),
        ('parsing', _('Parsing')),
        ('importing', _('Importing')),
        ('finalizing', _('Finalizing')),
        ('recovering', _('Recovering')),
        ('complete', _('Complete')),
    ]

    #: Stable external identifier.  Survives database row moves; safe to
    #: put in URLs and API responses.
    job_id = models.UUIDField(
        _('Job ID'), unique=True, default=uuid.uuid4, editable=False,
    )

    # -- Source identity --------------------------------------------------
    source_name = models.CharField(
        _('Source name'), max_length=1024,
    )
    source_path = models.TextField(_('Source path'), blank=True)
    source_size = models.BigIntegerField(
        _('Source size (bytes)'), null=True, blank=True,
    )
    source_hash = models.CharField(
        _('Source hash'), max_length=64, blank=True,
        help_text=_('SHA-256 of the source file(s), for change detection.'),
    )

    # -- Configuration ----------------------------------------------------
    import_format = models.CharField(
        _('Import format'), max_length=50, default='auto',
    )
    options = models.JSONField(
        _('Import options'), default=dict, blank=True,
    )

    # -- Lifecycle --------------------------------------------------------
    status = models.CharField(
        _('Status'), max_length=30, choices=STATUS_CHOICES,
        default='pending', db_index=True,
    )
    stage = models.CharField(
        _('Stage'), max_length=30, choices=STAGE_CHOICES,
        default='initializing',
    )

    # -- Timestamps -------------------------------------------------------
    created_at = models.DateTimeField(
        _('Created at'), default=timezone.now, db_index=True,
    )
    started_at = models.DateTimeField(
        _('Started at'), null=True, blank=True,
    )
    last_activity_at = models.DateTimeField(
        _('Last activity at'), null=True, blank=True,
    )
    completed_at = models.DateTimeField(
        _('Completed at'), null=True, blank=True,
    )

    # -- Counters (recalculated from ImportRecord rows on recovery) -------
    total_discovered = models.BigIntegerField(
        _('Total discovered'), default=0,
    )
    total_attempted = models.BigIntegerField(
        _('Total attempted'), default=0,
    )
    total_committed = models.BigIntegerField(
        _('Total committed'), default=0,
    )
    total_skipped = models.BigIntegerField(
        _('Total skipped'), default=0,
    )
    total_failed = models.BigIntegerField(
        _('Total failed'), default=0,
    )

    # -- Recovery ---------------------------------------------------------
    last_checkpoint = models.JSONField(
        _('Last checkpoint'), default=dict, blank=True,
    )
    recovery_attempts = models.IntegerField(
        _('Recovery attempts'), default=0,
    )
    last_error = models.TextField(_('Last error'), blank=True)

    # -- Concurrency ------------------------------------------------------
    worker_id = models.CharField(
        _('Worker ID'), max_length=255, blank=True,
    )
    lease_expires_at = models.DateTimeField(
        _('Lease expires at'), null=True, blank=True,
    )

    # -- Version ----------------------------------------------------------
    importer_version = models.CharField(
        _('Importer version'), max_length=50, default='2.0',
    )

    class Meta:
        ordering = ['-created_at']
        verbose_name = _('Import Job')
        verbose_name_plural = _('Import Jobs')
        indexes = [
            models.Index(
                fields=['status'], name='vault_importjob_status_idx',
            ),
            models.Index(
                fields=['-created_at'], name='vault_importjob_created_idx',
            ),
            models.Index(
                fields=['worker_id', 'lease_expires_at'],
                name='vault_importjob_lease_idx',
            ),
        ]

    def __str__(self):
        return f"ImportJob {self.job_id} ({self.status})"

    # -- Lifecycle helpers ------------------------------------------------

    def _refresh_lifecycle_state(self):
        """Refresh fields which may have changed in a competing worker."""
        self.refresh_from_db(fields=[
            'status', 'stage', 'started_at', 'last_activity_at',
            'completed_at', 'last_error', 'worker_id', 'lease_expires_at',
        ])

    def mark_started(self, worker_id=None):
        """Atomically transition an unowned/reserved job to processing."""
        now = timezone.now()
        expires = now + timezone.timedelta(seconds=IMPORT_LEASE_SECONDS)
        startable = (
            'pending', 'processing', 'paused', 'interrupted', 'failed',
            'completed_with_errors',
        )
        unowned = (
            models.Q(worker_id='')
            | models.Q(worker_id__isnull=True)
            | models.Q(lease_expires_at__isnull=True)
            | models.Q(lease_expires_at__lte=now)
        )
        query = ImportJob.objects.filter(pk=self.pk)
        if worker_id:
            # The same worker may mark a lease it just acquired as started;
            # an unowned row is also claimable here for legacy callers.
            query = query.filter(
                (
                    models.Q(status__in=startable)
                    & models.Q(
                        worker_id=worker_id,
                        lease_expires_at__gt=now,
                    )
                )
                | (models.Q(status__in=startable) & unowned)
            )
        else:
            query = query.filter(status__in=startable).filter(unowned)
        updates = {
            'status': 'processing',
            'stage': 'importing',
            'started_at': self.started_at or now,
            'last_activity_at': now,
            'completed_at': None,
        }
        if worker_id:
            updates.update({
                'worker_id': worker_id,
                'lease_expires_at': expires,
            })
        else:
            updates.update({
                'worker_id': '',
                'lease_expires_at': None,
            })
        if not query.update(**updates):
            self._refresh_lifecycle_state()
            return False
        self.status = 'processing'
        self.stage = 'importing'
        self.started_at = self.started_at or now
        self.last_activity_at = now
        self.completed_at = None
        if worker_id:
            self.worker_id = worker_id
            self.lease_expires_at = expires
        else:
            self.worker_id = ''
            self.lease_expires_at = None
        return True

    def mark_paused(self, worker_id=None):
        """Persist a pause request for a worker with a live lease."""
        now = timezone.now()
        query = ImportJob.objects.filter(pk=self.pk, status='processing')
        query = query.filter(lease_expires_at__gt=now).exclude(worker_id='')
        if worker_id:
            query = query.filter(worker_id=worker_id)
        if not query.update(status='paused', last_activity_at=now):
            self._refresh_lifecycle_state()
            return False
        self.status = 'paused'
        self.last_activity_at = now
        return True

    def mark_resumed(self):
        """Resume a paused worker in place when its lease is still live."""
        now = timezone.now()
        query = ImportJob.objects.filter(
            pk=self.pk,
            status='paused',
            worker_id__gt='',
            lease_expires_at__gt=now,
        )
        if not query.update(status='processing', last_activity_at=now):
            self._refresh_lifecycle_state()
            return False
        self.status = 'processing'
        self.last_activity_at = now
        return True

    def mark_cancelled(self, worker_id=None):
        """Cancel an active job; let a live worker flush its current batch."""
        now = timezone.now()
        active = ('pending', 'processing', 'paused')
        if worker_id:
            # The worker observes the durable cancellation on its next poll,
            # commits any already-parsed batch under this still-live lease,
            # then releases it. It can never finalize the job as completed.
            updated = ImportJob.objects.filter(
                pk=self.pk,
                status__in=(*active, 'cancelled'),
                worker_id=worker_id,
                lease_expires_at__gt=now,
            ).update(
                status='cancelled',
                completed_at=now,
                last_activity_at=now,
            )
            if not updated:
                self._refresh_lifecycle_state()
                return False
            self.status = 'cancelled'
            self.completed_at = now
            self.last_activity_at = now
            return True

        # Keep the fencing lease for an active worker so its current complete
        # batch can commit. Jobs with no live owner are cancelled and cleared
        # atomically; stale workers cannot reclaim them.
        updated = ImportJob.objects.filter(
            pk=self.pk,
            status__in=active,
            worker_id__gt='',
            lease_expires_at__gt=now,
        ).update(
            status='cancelled',
            completed_at=now,
            last_activity_at=now,
        )
        if updated:
            self.status = 'cancelled'
            self.completed_at = now
            self.last_activity_at = now
            return True

        unowned = (
            models.Q(worker_id='')
            | models.Q(worker_id__isnull=True)
            | models.Q(lease_expires_at__isnull=True)
            | models.Q(lease_expires_at__lte=now)
        )
        updated = ImportJob.objects.filter(
            pk=self.pk, status__in=active,
        ).filter(unowned).update(
            status='cancelled',
            completed_at=now,
            last_activity_at=now,
            worker_id='',
            lease_expires_at=None,
        )
        if not updated:
            self._refresh_lifecycle_state()
            return False
        self.status = 'cancelled'
        self.completed_at = now
        self.last_activity_at = now
        self.worker_id = ''
        self.lease_expires_at = None
        return True

    def mark_completed(self, has_errors=False, worker_id=None):
        """Complete only a live worker lease or an otherwise unowned job."""
        now = timezone.now()
        query = ImportJob.objects.filter(pk=self.pk)
        if worker_id:
            # A pause may race the worker's final source read. Completion is
            # still correct once every source position has a durable outcome;
            # cancellation remains terminal and is deliberately not accepted.
            query = query.filter(
                status__in=('processing', 'paused'),
                worker_id=worker_id,
                lease_expires_at__gt=now,
            )
        else:
            unowned = (
                models.Q(worker_id='')
                | models.Q(worker_id__isnull=True)
                | models.Q(lease_expires_at__isnull=True)
                | models.Q(lease_expires_at__lte=now)
            )
            query = query.filter(
                status__in=(
                    'pending', 'processing', 'paused', 'interrupted',
                    'failed', 'completed_with_errors',
                ),
            ).filter(unowned)
        status = 'completed_with_errors' if has_errors else 'completed'
        if not query.update(
            status=status,
            stage='complete',
            completed_at=now,
            last_activity_at=now,
            worker_id='',
            lease_expires_at=None,
        ):
            self._refresh_lifecycle_state()
            return False
        self.status = status
        self.stage = 'complete'
        self.completed_at = now
        self.last_activity_at = now
        self.worker_id = ''
        self.lease_expires_at = None
        return True

    def mark_failed(self, error_message='', worker_id=None):
        """Fail only an unowned job or the current live worker's job."""
        now = timezone.now()
        query = ImportJob.objects.filter(pk=self.pk)
        if worker_id:
            # If a pause request races an unrecoverable source/runtime error,
            # record the failure while still fencing to this live worker.
            # A cancellation is never overwritten by this transition.
            query = query.filter(
                status__in=('processing', 'paused'),
                worker_id=worker_id,
                lease_expires_at__gt=now,
            )
        else:
            unowned = (
                models.Q(worker_id='')
                | models.Q(worker_id__isnull=True)
                | models.Q(lease_expires_at__isnull=True)
                | models.Q(lease_expires_at__lte=now)
            )
            query = query.filter(
                status__in=(
                    'pending', 'processing', 'paused', 'interrupted',
                    'completed', 'completed_with_errors', 'failed',
                ),
            ).filter(unowned)
        message = error_message[:4000]
        if not query.update(
            status='failed',
            completed_at=now,
            last_activity_at=now,
            last_error=message,
            worker_id='',
            lease_expires_at=None,
        ):
            self._refresh_lifecycle_state()
            return False
        self.status = 'failed'
        self.completed_at = now
        self.last_activity_at = now
        self.last_error = message
        self.worker_id = ''
        self.lease_expires_at = None
        return True

    def mark_interrupted(self, worker_id=None):
        """Mark an interruption only while the caller still owns the job."""
        now = timezone.now()
        query = ImportJob.objects.filter(
            pk=self.pk, status__in=('processing', 'paused'),
        )
        if worker_id:
            query = query.filter(
                worker_id=worker_id, lease_expires_at__gt=now,
            )
        else:
            query = query.filter(
                models.Q(worker_id='')
                | models.Q(worker_id__isnull=True)
                | models.Q(lease_expires_at__isnull=True)
                | models.Q(lease_expires_at__lte=now)
            )
        if not query.update(status='interrupted', last_activity_at=now):
            self._refresh_lifecycle_state()
            return False
        self.status = 'interrupted'
        self.last_activity_at = now
        return True

    def refresh_counters(self):
        """Recalculate outcome counters from the authoritative ledger."""
        summary = self.records.aggregate(
            ledger_total=models.Count('id'),
            committed=models.Count('id', filter=models.Q(status='committed')),
            skipped=models.Count('id', filter=models.Q(status='skipped')),
            failed=models.Count('id', filter=models.Q(status='failed')),
            terminal=models.Count(
                'id', filter=models.Q(status__in=('committed', 'skipped', 'failed')),
            ),
        )
        # `total_discovered` comes from the immutable source manifest. Older
        # jobs (and callers feeding an iterable directly) may not have one,
        # so the durable ledger is the conservative fallback, never a reason
        # to reduce a known source total.
        if not self.source_hash:
            self.total_discovered = max(
                self.total_discovered, summary['ledger_total'] or 0,
            )
        self.total_attempted = summary['terminal'] or 0
        self.total_committed = summary['committed'] or 0
        self.total_skipped = summary['skipped'] or 0
        self.total_failed = summary['failed'] or 0
        self.last_activity_at = timezone.now()
        self.save(update_fields=[
            'total_discovered', 'total_attempted', 'total_committed',
            'total_skipped', 'total_failed', 'last_activity_at',
        ])

    @property
    def remaining_count(self):
        """Source lines not yet durably assigned a terminal outcome."""
        return max(0, self.total_discovered - self.total_attempted)

    def _has_replayable_source(self):
        """Whether the source can be safely re-read after this worker exits."""
        return bool(
            self.source_path
            or (self.options or {}).get('source_replayable', False)
        )

    def _has_live_lease(self, now=None):
        now = now or timezone.now()
        return bool(
            self.worker_id
            and self.lease_expires_at
            and self.lease_expires_at > now
        )

    def can_resume(self):
        """True for a live paused worker or an abandoned replayable source."""
        now = timezone.now()
        live_lease = self._has_live_lease(now)
        replayable = self._has_replayable_source()

        if self.status == 'paused':
            # A live streaming request is waiting at a durable batch boundary;
            # it can continue even for an upload whose bytes are not retained.
            return live_lease or (
                replayable and not live_lease
            )
        if self.status == 'pending':
            lease_expired = not live_lease
            has_recoverable_work = bool(
                self.source_path
                or (self.options or {}).get('source_replayable', False)
            )
            return lease_expired and has_recoverable_work
        if self.status in ('interrupted', 'failed'):
            return not live_lease and replayable
        if self.status == 'completed_with_errors':
            return (
                replayable
                and not live_lease
                and self.records.filter(status='failed', retryable=True).exists()
            )
        # A processing job whose lease has expired was abandoned by a
        # crashed worker; recovery may take it over only with a durable source.
        return (
            self.status == 'processing'
            and not live_lease
            and replayable
        )

    def can_cancel(self):
        """True if the job is still active, including while paused."""
        return self.status in ('pending', 'processing', 'paused')

    def can_pause(self):
        """True only while a processing worker owns a live lease."""
        return self.status == 'processing' and self._has_live_lease()

    @property
    def progress_percent(self):
        """Percent of manifest lines with a durable terminal ledger outcome."""
        if not self.total_discovered:
            return 100.0 if self.status in (
                'completed', 'completed_with_errors',
            ) else 0.0
        attempted = self.total_attempted
        if not attempted:
            # Older callers populated outcome counters directly before the
            # attempted counter existed; retain their progress semantics.
            attempted = (
                self.total_committed + self.total_skipped + self.total_failed
            )
        return min(
            100.0,
            (attempted / self.total_discovered) * 100,
        )


class ImportRecord(models.Model):
    """
    Ledger entry for one source record within an import job.

    Each source line (or deterministic position) gets exactly one
    ImportRecord.  The status tracks the outcome:

        pending     -- discovered but not yet processed
        processing  -- currently being parsed/inserted
        committed   -- durably written to the database
        skipped     -- invalid format, duplicate, or intentionally skipped
        failed      -- error during processing, eligible for retry

    The idempotency_key is a hash of the import job and source position,
    ensuring that retries cannot create duplicate ledger entries.
    """

    STATUS_CHOICES = [
        ('pending', _('Pending')),
        ('processing', _('Processing')),
        ('committed', _('Committed')),
        ('skipped', _('Skipped')),
        ('failed', _('Failed')),
    ]

    # No separate FK index: both (import_job, source_position) and
    # (import_job, status) below lead with this column, so a third index on
    # it alone only adds ledger write cost.
    import_job = models.ForeignKey(
        ImportJob, on_delete=models.CASCADE, related_name='records',
        verbose_name=_('Import job'), db_index=False,
    )
    source_position = models.BigIntegerField(
        _('Source position'),
        help_text=_('Line number or byte offset in the source.'),
    )
    source_hash = models.CharField(
        _('Source hash'), max_length=64,
        help_text=_('SHA-256 of the source line, for change detection.'),
    )
    source_line = models.TextField(
        _('Source line'), blank=True,
        help_text=_(
            'Legacy source text; new durable imports leave this blank because '
            'source lines can contain credentials.'
        ),
    )

    # No standalone status index: every status query is scoped to a job and
    # served by (import_job, status). The two single-column indexes had zero
    # scans after 7.4 M accounts and cost a write on every ledger row.
    status = models.CharField(
        _('Status'), max_length=20, choices=STATUS_CHOICES,
        default='pending',
    )

    # Reference to the created Account (if committed)
    account = models.ForeignKey(
        Account, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='import_records', verbose_name=_('Account'),
    )
    # Snapshot of the related row's identity. The SET_NULL FK preserves old
    # ledger entries if an Account is deleted; this hash lets verification
    # report that a formerly committed relationship has disappeared.
    account_fingerprint = models.CharField(
        _('Account fingerprint at commit'), max_length=64, blank=True,
    )

    # Error information (if failed). Failed rows are retryable only when the
    # importer has a deterministic source and the failure is transient.
    error_message = models.TextField(_('Error message'), blank=True)
    error_category = models.CharField(
        _('Error category'), max_length=50, blank=True,
    )
    retryable = models.BooleanField(_('Retryable'), default=False)

    processed_at = models.DateTimeField(
        _('Processed at'), null=True, blank=True,
    )

    # Idempotency: unique per (job, position)
    idempotency_key = models.CharField(
        _('Idempotency key'), max_length=128, unique=True,
        help_text=_(
            'Deterministic key to prevent duplicate processing on retry.'
        ),
    )

    class Meta:
        ordering = ['import_job', 'source_position']
        verbose_name = _('Import Record')
        verbose_name_plural = _('Import Records')
        indexes = [
            models.Index(
                fields=['import_job', 'status'],
                name='vault_importrec_job_status_idx',
            ),
            models.Index(
                fields=['import_job', 'source_position'],
                name='vault_importrec_job_pos_idx',
            ),
        ]

    def __str__(self):
        return (
            f"ImportRecord job={self.import_job_id} "
            f"pos={self.source_position} ({self.status})"
        )

    @staticmethod
    def compute_idempotency_key(job_id, source_position):
        """Deterministic idempotency key for a source record."""
        payload = f"{job_id}:{source_position}"
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:64]


class ImportRecoveryLog(models.Model):
    """
    Audit trail of recovery attempts for an interrupted import job.

    Each recovery run creates one ImportRecoveryLog entry, recording what
    was found, what was reconciled, and the outcome.
    """

    OUTCOME_CHOICES = [
        ('running', _('Running')),
        ('success', _('Success')),
        ('partial', _('Partial')),
        ('failed', _('Failed')),
    ]

    import_job = models.ForeignKey(
        ImportJob, on_delete=models.CASCADE, related_name='recovery_logs',
        verbose_name=_('Import job'),
    )
    attempt_number = models.IntegerField(_('Attempt number'))
    recovery_started_at = models.DateTimeField(
        _('Recovery started at'), default=timezone.now,
    )
    recovery_completed_at = models.DateTimeField(
        _('Recovery completed at'), null=True, blank=True,
    )

    # Reconciliation results
    records_committed = models.BigIntegerField(
        _('Records committed'), default=0,
    )
    records_pending = models.BigIntegerField(
        _('Records pending'), default=0,
    )
    records_failed = models.BigIntegerField(
        _('Records failed'), default=0,
    )
    reconciliation_details = models.JSONField(
        _('Reconciliation details'), default=dict, blank=True,
    )

    outcome = models.CharField(
        _('Outcome'), max_length=20, choices=OUTCOME_CHOICES,
        default='success',
    )
    notes = models.TextField(_('Notes'), blank=True)

    class Meta:
        ordering = ['-recovery_started_at']
        verbose_name = _('Import Recovery Log')
        verbose_name_plural = _('Import Recovery Logs')

    def __str__(self):
        return (
            f"RecoveryLog job={self.import_job_id} "
            f"attempt={self.attempt_number} ({self.outcome})"
        )
