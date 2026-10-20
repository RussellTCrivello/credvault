"""Operational metrics for durable imports.

The importer used to expose only a row counter on the job. Operating an
import at scale needs more: how fast batches commit, how long the database
takes per batch, and whether parsing or the database is the limiting stage.
This module keeps a small in-process aggregate per job and writes it as one
structured log line every ``VAULT_IMPORT_METRICS_INTERVAL`` seconds
(default 30, ``0`` disables the periodic line) plus a final summary.

Security: only counts, byte totals, durations and the job UUID are recorded.
Source lines, parsed values, fingerprints, hashes and error text never enter
a metric, so enabling the log adds no new copy of any credential.

Metrics are best effort. Recording never raises into the import and takes a
lock only around a few integer updates.
"""

import logging
import os
import threading
import time

logger = logging.getLogger('vault.import.metrics')

DEFAULT_INTERVAL_SECONDS = 30.0
_LATENCY_SAMPLES = 512


def metrics_interval():
    """Seconds between periodic log lines; 0 disables them."""
    raw = os.environ.get('VAULT_IMPORT_METRICS_INTERVAL', '')
    try:
        value = float(raw) if raw.strip() else DEFAULT_INTERVAL_SECONDS
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS
    return max(0.0, min(value, 3600.0))


def _percentile(sorted_values, fraction):
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, int(round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


class ImportMetrics:
    """Aggregate counters for one import job (thread-safe, bounded memory)."""

    def __init__(self, job_id='', interval=None, clock=time.monotonic):
        self.job_id = str(job_id)
        self._clock = clock
        self._interval = metrics_interval() if interval is None else interval
        self._lock = threading.Lock()
        self._started = clock()
        self._last_log = self._started
        self.batches = 0
        self.items = 0
        self.valid_rows = 0
        self.db_seconds = 0.0
        self.max_batch_seconds = 0.0
        self.batch_bytes = 0
        self.parse_pending_bytes = 0
        self.max_parse_pending_bytes = 0
        self._recent = []  # ring of recent per-batch DB seconds

    # -- recording ---------------------------------------------------------
    def record_batch(self, items, valid_rows, db_seconds, batch_bytes=0):
        with self._lock:
            self.batches += 1
            self.items += int(items)
            self.valid_rows += int(valid_rows)
            self.db_seconds += db_seconds
            self.batch_bytes += int(batch_bytes)
            self.max_batch_seconds = max(self.max_batch_seconds, db_seconds)
            self._recent.append(db_seconds)
            if len(self._recent) > _LATENCY_SAMPLES:
                del self._recent[0]
        self._maybe_log()

    def set_parse_backlog(self, pending_bytes):
        """Gauge: bytes of raw lines waiting for / inside parse workers."""
        with self._lock:
            self.parse_pending_bytes = int(pending_bytes)
            if pending_bytes > self.max_parse_pending_bytes:
                self.max_parse_pending_bytes = int(pending_bytes)

    # -- reading -----------------------------------------------------------
    def snapshot(self):
        with self._lock:
            elapsed = max(self._clock() - self._started, 1e-9)
            recent = sorted(self._recent)
            return {
                'job_id': self.job_id,
                'elapsed_seconds': round(elapsed, 3),
                'batches': self.batches,
                'items': self.items,
                'valid_rows': self.valid_rows,
                'items_per_second': round(self.items / elapsed, 1),
                'db_seconds': round(self.db_seconds, 3),
                # Share of wall time spent inside batch transactions. Near 1
                # means the database is the bottleneck; low values mean the
                # reader / parser is starving it.
                'db_busy_ratio': round(min(self.db_seconds / elapsed, 1.0), 3),
                'batch_seconds_p50': round(_percentile(recent, 0.5), 4),
                'batch_seconds_p95': round(_percentile(recent, 0.95), 4),
                'batch_seconds_max': round(self.max_batch_seconds, 4),
                'batch_bytes': self.batch_bytes,
                'parse_pending_bytes': self.parse_pending_bytes,
                'parse_pending_bytes_max': self.max_parse_pending_bytes,
            }

    # -- logging -----------------------------------------------------------
    def _format(self, snap, event):
        return 'import_metrics event=%s ' % event + ' '.join(
            '%s=%s' % (key, value) for key, value in snap.items()
        )

    def _maybe_log(self):
        if self._interval <= 0:
            return
        now = self._clock()
        if now - self._last_log < self._interval:
            return
        self._last_log = now
        try:
            logger.info(self._format(self.snapshot(), 'progress'))
        except Exception:  # metrics must never break an import
            pass

    def log_summary(self):
        if self._interval <= 0:
            return
        try:
            logger.info(self._format(self.snapshot(), 'summary'))
        except Exception:
            pass
