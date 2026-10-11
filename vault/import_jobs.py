"""
Live, byte-accurate progress for file and folder imports.

An :class:`ImportJob` is the single source of truth the import screen polls
while an ingest runs. Every number it exposes comes from the importer itself
-- bytes read, lines classified, rows written, files finished -- so the bar
reflects measured work instead of a guess:

* percent is ``bytes consumed / bytes known``, which is exact for one file and
  for a ten-thousand-file folder tree alike;
* ``lines`` always equals ``blank + invalid + valid``, and ``valid`` equals
  ``created + duplicates + pending``, so the counters can be cross-checked at a
  glance rather than trusted;
* rate and ETA come from a smoothed sample taken when a client polls, so the
  per-line loop never pays for timing.

The job is transport-agnostic: the web import view, the folder dry-run, and
the ``importfolder`` command can all attach to the same object. Progress lives
in a small in-process registry guarded by a lock, and finished jobs are pruned
so a long-lived server never accumulates them.
"""

import os
import threading
import time
import uuid

from django.utils.translation import gettext_lazy as _lazy

from .importer import (
    ImportCancelled, ImportStats, collect_files_from_path, file_size,
    import_stream,
)

# Job lifecycle states.
STATE_WAITING = 'waiting'      # browser transfer still in flight
STATE_RUNNING = 'running'
STATE_DONE = 'done'
STATE_CANCELLED = 'cancelled'
STATE_ERROR = 'error'

# Display stages, in the order a successful import walks through them.
STAGE_QUEUED = 'queued'
STAGE_UPLOADING = 'uploading'
STAGE_SCANNING = 'scanning'
STAGE_IMPORTING = 'importing'
STAGE_FINALIZING = 'finalizing'
STAGE_COMPLETED = 'completed'
STAGE_CANCELLED = 'cancelled'
STAGE_FAILED = 'failed'

STAGE_LABELS = {
    STAGE_QUEUED: _lazy("Queued"),
    STAGE_UPLOADING: _lazy("Receiving files"),
    STAGE_SCANNING: _lazy("Listing files on disk"),
    STAGE_IMPORTING: _lazy("Reading and storing records"),
    STAGE_FINALIZING: _lazy("Finishing up"),
    STAGE_COMPLETED: _lazy("Import complete"),
    STAGE_CANCELLED: _lazy("Import stopped"),
    STAGE_FAILED: _lazy("Import failed"),
}

# The four steps drawn as the progress rail. ``completed``/``cancelled``/
# ``failed`` replace the rail with their own summary state.
STAGE_STEPS = [STAGE_UPLOADING, STAGE_SCANNING, STAGE_IMPORTING,
               STAGE_FINALIZING]

FILE_QUEUED = 'queued'
FILE_ACTIVE = 'active'
FILE_DONE = 'done'
FILE_FAILED = 'failed'
FILE_STOPPED = 'stopped'

# How many files a status payload describes in detail around the active one.
FILE_WINDOW = 60
# Finished jobs stay readable for a while, then fall out of the registry.
FINISHED_TTL = 45 * 60
MAX_FINISHED_JOBS = 20
MAX_WARNINGS = 20
# Upper bound on the file roster a prepared job may hold; a folder with more
# files than this is counted exactly but listed only up to the cap.
MAX_LISTED_FILES = 20000
# A job that has not reported anything for this long while running is either a
# dead worker or a browser that went away; say so rather than show a confident,
# frozen percentage.
STALE_AFTER = 120

_TERMINAL_STATES = {STATE_DONE, STATE_CANCELLED, STATE_ERROR}


def _count(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _percent_of(done, total, floor=0.0):
    """Percentage of *done* out of *total*, or ``None`` if unknowable."""
    if not total:
        return None
    return max(floor, min(100.0, round(min(done / total, 1.0) * 100, 1)))


class Source:
    """
    One file to ingest: either an upload already held by Django or a path on
    the server's disk. ``size`` is what makes the percentage real, so it is
    captured up front whenever the browser or the filesystem knows it.
    """

    __slots__ = ('name', 'size', 'handle', 'path', 'relpath')

    def __init__(self, name, *, size=None, handle=None, path=None,
                 relpath=None):
        self.name = name
        self.size = size
        self.handle = handle
        self.path = path
        self.relpath = relpath or name

    @classmethod
    def from_upload(cls, uploaded_file, relpath=None):
        return cls(
            name=getattr(uploaded_file, 'name', '') or 'upload',
            size=getattr(uploaded_file, 'size', None),
            handle=uploaded_file,
            relpath=relpath,
        )

    @classmethod
    def from_path(cls, path, *, size=None, root=None):
        relpath = path
        if root and path.startswith(root):
            relpath = os.path.relpath(path, root)
        return cls(
            name=os.path.basename(path),
            size=file_size(path) if size is None else size,
            path=path,
            relpath=relpath.replace(os.sep, '/'),
        )

    @property
    def is_upload(self):
        return self.handle is not None

    def open(self):
        """Return a binary stream positioned at the start of the file."""
        if self.handle is not None:
            try:
                if self.handle.seekable():
                    self.handle.seek(0)
            except (AttributeError, OSError, ValueError):
                pass
            return self.handle
        return open(self.path, 'rb')


class ImportJob:
    """
    Progress state for a single import run.

    The importer calls :meth:`file_bytes`, :meth:`note` and :meth:`stopped`
    while it works; the view calls :meth:`begin_file` / :meth:`end_file` per
    file and :meth:`snapshot` for every poll. All mutations share one lock, so
    a poll never observes half-updated counters.
    """

    def __init__(self, job_id, *, label='', source_kind='upload',
                 sources=None, total_bytes=None, register_files=True):
        self.id = job_id
        self.label = label
        self.source_kind = source_kind
        self.sources = list(sources or [])

        self.state = STATE_WAITING
        self.stage = STAGE_QUEUED
        self.error = None
        self.warnings = []
        self.warnings_dropped = 0

        self.files_done = 0
        self.files_failed = 0
        self.bytes_done = 0
        self.bytes_total = 0

        self.lines = 0
        self.blank = 0
        self.invalid = 0
        self.valid = 0
        self.created = 0
        self.duplicates = 0

        self.started_monotonic = time.monotonic()
        self.started_at = time.time()
        self.updated_at = self.started_monotonic
        self.finished_monotonic = None
        self.duration_seconds = None

        self.files = []
        self.current = None
        self._index_by_path = {}
        self._scanned_files = None
        self._declared_files = len(self.sources)
        self.scanned = None

        if register_files:
            for index, source in enumerate(self.sources):
                self.add_file(source, index=index + 1)
        self.bytes_total = (
            _count(total_bytes) if total_bytes is not None
            else sum(_count(source.size) for source in self.sources)
        )

        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._percent_shown = 0.0
        self._rate = None
        self._line_rate = None
        self._sample_at = self.started_monotonic
        self._sample_bytes = 0
        self._sample_lines = 0

    # ------------------------------------------------------------------
    # File roster
    # ------------------------------------------------------------------
    def add_file(self, source, *, index=None):
        """Register a file to import, up front or while scanning a tree."""
        entry = {
            'index': len(self.files) + 1 if index is None else index,
            'name': source.name,
            'path': source.relpath,
            'bytes_total': _count(source.size),
            'bytes_done': 0,
            'percent': 0.0,
            'lines': 0,
            'blank': 0,
            'invalid': 0,
            'valid': 0,
            'created': 0,
            'duplicates': 0,
            'state': FILE_QUEUED,
            'seconds': None,
            'error': None,
            '_started': None,
        }
        self.files.append(entry)
        if source.path:
            self._index_by_path[source.path] = entry
        self.bytes_total += entry['bytes_total']
        return entry

    def file_entry(self, index, source):
        """The row that tracks *source*, created if the scan did not know it."""
        if source.path and source.path in self._index_by_path:
            return self._index_by_path[source.path]
        if index < len(self.files):
            return self.files[index]
        return self.add_file(source, index=index + 1)

    def begin_file(self, source, *, entry=None):
        entry = entry or self.file_entry(len(self.files), source)
        with self._lock:
            self.current = entry
            self.state = STATE_RUNNING
            if self.stage in (STAGE_QUEUED, STAGE_UPLOADING, STAGE_SCANNING):
                self.stage = STAGE_IMPORTING
            entry['state'] = FILE_ACTIVE
            entry['_started'] = time.monotonic()
            self.updated_at = entry['_started']
        return entry

    def end_file(self, *, stats=None, error=None, cancelled=False):
        """Close the active file row and fold its :class:`ImportStats` in."""
        with self._lock:
            entry = self.current
            if entry is not None:
                if stats is not None:
                    self._absorb(entry, stats)
                if entry['_started'] is not None:
                    entry['seconds'] = round(
                        time.monotonic() - entry['_started'], 3
                    )
                if error:
                    entry['state'] = FILE_FAILED
                    entry['error'] = error
                    self.files_failed += 1
                elif cancelled:
                    entry['state'] = FILE_STOPPED
                else:
                    entry['state'] = FILE_DONE
                    entry['percent'] = 100.0
                    entry['bytes_done'] = max(
                        entry['bytes_done'], entry['bytes_total']
                    )
                self.files_done += 1
            self.current = None
            self.updated_at = time.monotonic()

    def _absorb(self, entry, stats):
        """Fold an ImportStats into a file row without double counting."""
        for key, value in (
            ('lines', stats.lines), ('blank', stats.blank),
            ('invalid', stats.invalid), ('valid', stats.valid),
            ('created', stats.created), ('duplicates', stats.duplicates),
        ):
            delta = _count(value) - entry[key]
            if delta > 0:
                entry[key] = _count(value)
                setattr(self, key, getattr(self, key) + delta)

    def replace_sources(self, sources):
        """
        Adopt the files that are actually about to be imported.

        The screen may have registered a roster earlier (from the browser's own
        file list, or from a dry-run scan of a server path). Once the import
        starts, that roster is replaced with what really arrived, so the
        per-file rows and the byte denominator describe exactly the work being
        done -- never the intention.
        """
        with self._lock:
            self.sources = list(sources)
            self.files = []
            self._index_by_path = {}
            self.bytes_total = 0
            self._declared_files = len(self.sources)
            for index, source in enumerate(self.sources):
                self.add_file(source, index=index + 1)

    def load_roster(self, entries):
        """Fill the file list from ``{'name', 'path', 'bytes'}`` dicts."""
        with self._lock:
            self.files = []
            self._index_by_path = {}
            self.bytes_total = 0
            for index, entry in enumerate(entries, start=1):
                source = Source(
                    name=str(entry.get('name') or '')[:255] or 'upload',
                    size=entry.get('bytes') or entry.get('size'),
                    path=entry.get('path'),
                    relpath=(
                        entry.get('relpath') or entry.get('path')
                        or entry.get('name')
                    ),
                )
                self.add_file(source, index=index)
            self._declared_files = len(self.files)
            self._scanned_files = len(self.files)

    def remember_scan(self, folder_path, sources, total_bytes):
        """Cache a dry-run scan so the import that follows does not re-walk."""
        with self._lock:
            self.scanned = {
                'path': folder_path,
                'sources': list(sources),
                'bytes_total': _count(total_bytes),
                'at': time.time(),
            }

    def reuse_scan(self, folder_path):
        scan = getattr(self, 'scanned', None)
        if scan and scan['path'] == folder_path:
            return scan['sources'], scan['bytes_total']
        return None

    # ------------------------------------------------------------------
    # Tracker protocol used by vault.importer
    # ------------------------------------------------------------------
    def file_bytes(self, count):
        """Bytes consumed from the file currently being read."""
        if not count:
            return
        with self._lock:
            self.bytes_done += count
            if self.current is not None:
                entry = self.current
                entry['bytes_done'] += count
                if entry['bytes_total']:
                    entry['percent'] = _percent_of(
                        entry['bytes_done'], entry['bytes_total'],
                        floor=entry['percent'],
                    )
            self.updated_at = time.monotonic()

    def note(self, **counts):
        """Line/record deltas pushed by the importer at each batch flush."""
        lines = _count(counts.get('lines'))
        blank = _count(counts.get('blank'))
        invalid = _count(counts.get('invalid'))
        valid = _count(counts.get('valid'))
        created = _count(counts.get('created'))
        duplicates = _count(counts.get('duplicate'))
        if not (lines or blank or invalid or valid or created or duplicates):
            return
        with self._lock:
            self.lines += lines
            self.blank += blank
            self.invalid += invalid
            self.valid += valid
            self.created += created
            self.duplicates += duplicates
            entry = self.current
            if entry is not None:
                entry['lines'] += lines
                entry['blank'] += blank
                entry['invalid'] += invalid
                entry['valid'] += valid
                entry['created'] += created
                entry['duplicates'] += duplicates
            self.updated_at = time.monotonic()

    def stopped(self):
        return self._cancel.is_set()

    def file_error(self, path, message):
        self.warn("%s: %s" % (os.path.basename(path), message))

    def warn(self, message):
        with self._lock:
            if len(self.warnings) < MAX_WARNINGS:
                self.warnings.append(str(message))
            else:
                self.warnings_dropped += 1

    def cancel(self):
        """Ask the running import to stop at the next batch boundary."""
        self._cancel.set()
        with self._lock:
            if self.state == STATE_WAITING:
                self.state = STATE_CANCELLED
                self.stage = STAGE_CANCELLED
                self._close_out()
            else:
                self.updated_at = time.monotonic()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def mark_waiting(self, *, total_bytes=None, files_total=None):
        with self._lock:
            self.state = STATE_WAITING
            self.stage = STAGE_UPLOADING
            if total_bytes is not None:
                self.bytes_total = _count(total_bytes)
            if files_total is not None:
                self._declared_files = _count(files_total)
            self.started_monotonic = time.monotonic()
            self.started_at = time.time()
            self.updated_at = self.started_monotonic

    def mark_scanning(self):
        with self._lock:
            self.state = STATE_RUNNING
            self.stage = STAGE_SCANNING
            self.started_monotonic = time.monotonic()
            self.started_at = time.time()
            self._sample_at = self.started_monotonic
            self.updated_at = self.started_monotonic

    def report_scan(self, files_found, total_bytes):
        with self._lock:
            self._scanned_files = files_found
            if total_bytes:
                self.bytes_total = max(self.bytes_total, _count(total_bytes))
            self.updated_at = time.monotonic()

    def mark_finalizing(self):
        with self._lock:
            self.stage = STAGE_FINALIZING
            self.current = None
            self.updated_at = time.monotonic()

    def finish(self):
        with self._lock:
            if self._cancel.is_set():
                self.state = STATE_CANCELLED
                self.stage = STAGE_CANCELLED
            else:
                self.state = STATE_DONE
                self.stage = STAGE_COMPLETED
            self._close_out()

    def fail(self, message):
        with self._lock:
            self.state = STATE_ERROR
            self.stage = STAGE_FAILED
            self.error = str(message or '') or str(
                _lazy("The import could not be completed.")
            )
            self._close_out()

    def _close_out(self):
        self.finished_monotonic = time.monotonic()
        self.duration_seconds = round(
            self.finished_monotonic - self.started_monotonic, 2
        )
        self.current = None
        self.updated_at = self.finished_monotonic

    # ------------------------------------------------------------------
    # Derived numbers
    # ------------------------------------------------------------------
    def _sample_rate(self, now):
        """
        Smoothed throughput, sampled when somebody asks.

        Polling is the only timing cost: the import loop never reads a clock.
        A smoothed rate is shown while the run is live (it is what an honest
        "remaining time" must be based on) and the plain average afterwards.
        """
        window = now - self._sample_at
        if window >= 0.2:
            instant_bytes = (self.bytes_done - self._sample_bytes) / window
            instant_lines = (self.lines - self._sample_lines) / window
            self._rate = instant_bytes if self._rate is None \
                else self._rate * 0.65 + instant_bytes * 0.35
            self._line_rate = instant_lines if self._line_rate is None \
                else self._line_rate * 0.65 + instant_lines * 0.35
            self._sample_at = now
            self._sample_bytes = self.bytes_done
            self._sample_lines = self.lines
        return self._rate or 0.0, self._line_rate or 0.0

    def snapshot(self, *, file_offset=None, include_files=True):
        """JSON-safe view of the job, computed under the lock."""
        with self._lock:
            now = self.finished_monotonic or time.monotonic()
            elapsed = max(now - self.started_monotonic, 0.001)
            live = self.state not in _TERMINAL_STATES
            if live:
                rate, line_rate = self._sample_rate(now)
            else:
                rate = self._rate or 0.0
                line_rate = self._line_rate or 0.0
            if not rate:
                rate = self.bytes_done / elapsed
            if not line_rate:
                line_rate = self.lines / elapsed

            files_total_for_percent = len(self.files) or self._declared_files
            if self.state == STATE_DONE:
                percent = 100.0
            elif self.bytes_total:
                percent = _percent_of(self.bytes_done, self.bytes_total)
            elif files_total_for_percent:
                # Sizes unknown (an empty folder, a streamed upload with no
                # length): completed files are the only honest denominator.
                percent = _percent_of(self.files_done, files_total_for_percent)
            else:
                percent = None
            if percent is not None and live:
                # A bar must never walk backwards because of rounding.
                percent = max(percent, self._percent_shown)
                self._percent_shown = percent

            eta = None
            if live and rate > 4096 and percent is not None and percent < 100:
                remaining_bytes = max(self.bytes_total - self.bytes_done, 0)
                eta = round(min(remaining_bytes / rate, 24 * 3600), 1)

            files_total = len(self.files) or self._declared_files
            files_found = (self._scanned_files
                           if self._scanned_files is not None else files_total)
            seconds_since_update = round(now - self.updated_at, 2)
            data = {
                'id': self.id,
                'label': self.label,
                'source_kind': self.source_kind,
                'state': self.state,
                'stage': self.stage,
                'stage_label': str(STAGE_LABELS.get(self.stage, self.stage)),
                'stages': STAGE_STEPS,
                'stage_index': self._stage_index(),
                'error': self.error,
                'warnings': list(self.warnings),
                'warnings_dropped': self.warnings_dropped,
                'cancel_requested': self._cancel.is_set(),
                'finished': self.state in _TERMINAL_STATES,
                'percent': percent,
                'files_total': files_total,
                'files_found': files_found,
                'files_done': self.files_done,
                'files_failed': self.files_failed,
                'files_remaining': max(files_total - self.files_done, 0),
                'bytes_total': self.bytes_total,
                'bytes_done': self.bytes_done,
                'bytes_remaining': max(self.bytes_total - self.bytes_done, 0),
                'lines': self.lines,
                'blank': self.blank,
                'invalid': self.invalid,
                'valid': self.valid,
                'created': self.created,
                'duplicates': self.duplicates,
                'pending': max(self.valid - self.created - self.duplicates, 0),
                'elapsed_seconds': round(elapsed, 2),
                'seconds_since_update': seconds_since_update,
                'duration_seconds': self.duration_seconds,
                'lines_per_second': max(int(line_rate), 0),
                'bytes_per_second': max(int(rate), 0),
                'eta_seconds': eta,
                'stale': bool(live and seconds_since_update > STALE_AFTER),
                'current_file': None,
                'files': [],
                'files_window': [0, 0],
            }

            if self.current is not None:
                current = {
                    key: self.current[key]
                    for key in (
                        'index', 'name', 'path', 'bytes_total', 'bytes_done',
                        'percent', 'lines', 'created', 'duplicates', 'state',
                    )
                }
                current['label'] = str(_lazy("File %(n)s of %(total)s") % {
                    'n': current['index'], 'total': files_total or 1,
                })
                data['current_file'] = current

            if include_files:
                window = self._file_window(file_offset)
                data['files'] = [
                    {key: value for key, value in entry.items()
                     if not key.startswith('_')}
                    for entry in window
                ]
                data['files_window'] = [
                    window[0]['index'] if window else 0,
                    window[-1]['index'] if window else 0,
                ]
            return data

    def _stage_index(self):
        try:
            return STAGE_STEPS.index(self.stage)
        except ValueError:
            if self.state == STATE_DONE:
                return len(STAGE_STEPS)
            return 0

    def _file_window(self, file_offset):
        if not self.files:
            return []
        if file_offset is None:
            anchor = (self.current['index'] - 1) if self.current \
                else self.files_done
            start = max(0, min(anchor - FILE_WINDOW // 3,
                               len(self.files) - FILE_WINDOW))
        else:
            start = max(0, min(int(file_offset),
                               max(len(self.files) - 1, 0)))
        return self.files[start:start + FILE_WINDOW]

    @property
    def is_terminal(self):
        return self.state in _TERMINAL_STATES

    @property
    def stats(self):
        """The same counters as an :class:`~vault.importer.ImportStats`."""
        return ImportStats(
            lines=self.lines, blank=self.blank, invalid=self.invalid,
            valid=self.valid, duplicates=self.duplicates,
            created=self.created,
        )

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_registry = {}
_registry_order = []
_registry_lock = threading.Lock()


def create(*, label='', source_kind='upload', sources=None, total_bytes=None,
           register_files=True, waiting=True, files_total=None):
    """Register a new job and return it."""
    job = ImportJob(
        uuid.uuid4().hex, label=label, source_kind=source_kind,
        sources=sources, total_bytes=total_bytes,
        register_files=register_files,
    )
    if waiting:
        job.mark_waiting(
            total_bytes=job.bytes_total,
            files_total=files_total if files_total is not None
            else len(job.files),
        )
    with _registry_lock:
        _prune_locked()
        _registry[job.id] = job
        _registry_order.append(job.id)
    return job


def get(job_id):
    if not job_id:
        return None
    with _registry_lock:
        return _registry.get(job_id)


def forget(job_id):
    with _registry_lock:
        _registry.pop(job_id, None)
        if job_id in _registry_order:
            _registry_order.remove(job_id)


def prune():
    with _registry_lock:
        _prune_locked()


def reset():
    """Drop every job -- used by the test suite between cases."""
    with _registry_lock:
        _registry.clear()
        del _registry_order[:]


def _prune_locked():
    """Keep the registry bounded: finished jobs expire, dead ones are dropped."""
    now = time.monotonic()
    expired = [
        job_id for job_id in list(_registry_order)
        if _registry[job_id].state in _TERMINAL_STATES
        and _registry[job_id].finished_monotonic is not None
        and now - _registry[job_id].finished_monotonic > FINISHED_TTL
    ]
    expired += [
        job_id for job_id in list(_registry_order)
        if job_id not in expired
        and _registry[job_id].state == STATE_WAITING
        and now - _registry[job_id].started_monotonic > FINISHED_TTL
    ]
    for job_id in expired:
        _registry.pop(job_id, None)
        if job_id in _registry_order:
            _registry_order.remove(job_id)

    finished = [
        job_id for job_id in list(_registry_order)
        if job_id in _registry
        and _registry[job_id].state in _TERMINAL_STATES
    ]
    while len(finished) > MAX_FINISHED_JOBS:
        job_id = finished.pop(0)
        _registry.pop(job_id, None)
        _registry_order.remove(job_id)


# ---------------------------------------------------------------------------
# Running a job
# ---------------------------------------------------------------------------

def run(job, sources=None, *, close_uploads=False):
    """
    Execute *job* in the calling thread, updating it as it goes.

    Running inline (rather than in a background worker) means the uploaded
    files stay alive for the whole ingest and the progress endpoint can still
    read the counters from a second request: the browser polls
    ``vault:import_job_status`` while the import POST is in flight.

    Upload handles are left open on purpose -- Django spools them to a
    temporary file and removes it when the request ends, so closing them here
    would only risk a second read of a file the user may want to retry. Pass
    ``close_uploads=True`` when the caller owns the handles.
    """
    if sources is None:
        sources = list(job.sources)
    with job._lock:
        job.sources = sources
        job.state = STATE_RUNNING
        job.stage = STAGE_IMPORTING
        job.started_monotonic = time.monotonic()
        job.started_at = time.time()
        job._sample_at = job.started_monotonic
        job.updated_at = job.started_monotonic

    totals = ImportStats()
    try:
        for index, source in enumerate(sources):
            if job.stopped():
                raise ImportCancelled()
            entry = job.file_entry(index, source)
            job.begin_file(source, entry=entry)
            file_stats = ImportStats()
            error = None
            cancelled = False
            stream = None
            try:
                stream = source.open()
                file_stats = import_stream(stream, tracker=job)
            except ImportCancelled:
                cancelled = True
            except OSError as exc:
                error = str(exc) or exc.__class__.__name__
                job.warn("%s: %s" % (source.relpath, error))
            except Exception as exc:  # pragma: no cover - defensive
                error = str(exc) or exc.__class__.__name__
                job.warn("%s: %s" % (source.relpath, error))
            finally:
                if stream is not None and source.path is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            totals.merge(file_stats)
            job.end_file(stats=file_stats, error=error, cancelled=cancelled)
            if cancelled:
                raise ImportCancelled()
        job.mark_finalizing()
        job.finish()
    except ImportCancelled:
        job.finish()
    except Exception as exc:  # pragma: no cover - defensive
        job.fail(str(exc) or exc.__class__.__name__)
    finally:
        if close_uploads:
            for source in sources:
                if source.handle is not None:
                    try:
                        source.handle.close()
                    except (AttributeError, OSError, ValueError):
                        pass
    return totals


def scan(folder_path, *, ext='*', on_found=None, stats=None):
    """
    List the files a folder-path import would read.

    Returns ``(paths, total_bytes)``; ``on_found(count, total_bytes)`` is
    called while walking, so the UI can report on enormous trees before a
    single record is parsed, and ``stats`` receives the number of files that
    were seen but ignored.
    """
    paths = collect_files_from_path(
        folder_path, ext=ext, on_found=on_found, stats=stats
    )
    total_bytes = 0
    for path in paths:
        size = file_size(path)
        if size:
            total_bytes += size
    return paths, total_bytes


def sources_for_paths(paths, *, root=None):
    return [Source.from_path(path, root=root) for path in paths]
