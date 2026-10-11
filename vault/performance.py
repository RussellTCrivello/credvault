"""
Speed profiles for imports and searches.

The workspace has one *import* mode and one *query* mode, chosen on the
Settings page and stored in ``PerformanceSettings``. Every number that a mode
changes lives in this module, so the trade-off of each mode is visible in one
place:

* Balanced -- counts lines before importing so the progress bar is exact,
  and commits every batch durably. Records are streamed with ``COPY`` into a
  staging table on PostgreSQL and written with set-based statements; SQLite
  uses the ORM writer. This is the default.
* Fast -- the same writer with asynchronous commits and bigger batches, and no
  separate line count: progress is measured in bytes, so the file is not read
  twice.
* Maximum speed -- Fast, plus lines are parsed in parallel worker processes,
  bigger batches, and more memory per transaction.

Query modes change how many matches are counted exactly and how many rows a
result page reads. They never change which records match.
"""

import os
from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _


@dataclass(frozen=True)
class ImportProfile:
    key: str
    label: object
    description: object
    batch_size: int          # lines per transaction
    use_copy: bool           # PostgreSQL COPY path (ignored on SQLite)
    count_lines: bool        # count lines first for an exact progress bar
    parse_workers: int       # processes that parse lines
    fast_commit: bool        # SET LOCAL synchronous_commit = off (PostgreSQL)
    work_mem: str | None     # per-transaction memory for joins (PostgreSQL)


@dataclass(frozen=True)
class QueryProfile:
    key: str
    label: object
    description: object
    page_size: int           # records shown per page
    count_cap: int           # exact match counts stop here ("N+" beyond)
    literal_id_limit: int    # reference ids inlined into one query (PostgreSQL)
    api_limit: int           # rows returned by the JSON search endpoint


def default_parse_workers():
    return max(1, min(8, os.cpu_count() or 1))


IMPORT_PROFILES = {
    'balanced': ImportProfile(
        key='balanced',
        label=_('Balanced'),
        description=_(
            'Exact progress bar, one process, and every commit fully durable. '
            'The default.'
        ),
        batch_size=50_000,
        use_copy=True,
        count_lines=True,
        parse_workers=1,
        fast_commit=False,
        work_mem=None,
    ),
    'fast': ImportProfile(
        key='fast',
        label=_('Fast'),
        description=_(
            'Skips the line count and commits asynchronously in larger batches; '
            'progress is measured by bytes read.'
        ),
        batch_size=200_000,
        use_copy=True,
        count_lines=False,
        parse_workers=1,
        fast_commit=True,
        work_mem='128MB',
    ),
    'maximum': ImportProfile(
        key='maximum',
        label=_('Maximum speed'),
        description=_(
            'Fast, plus parallel parsing across CPU cores and larger batches.'
        ),
        batch_size=400_000,
        use_copy=True,
        count_lines=False,
        parse_workers=default_parse_workers(),
        fast_commit=True,
        work_mem='256MB',
    ),
}

QUERY_PROFILES = {
    'balanced': QueryProfile(
        key='balanced',
        label=_('Balanced'),
        description=_('Exact counts up to 100,000 matches.'),
        page_size=100,
        count_cap=100_000,
        literal_id_limit=1_000,
        api_limit=50,
    ),
    'fast': QueryProfile(
        key='fast',
        label=_('Fast'),
        description=_('Exact counts up to 10,000 matches.'),
        page_size=100,
        count_cap=10_000,
        literal_id_limit=1_000,
        api_limit=25,
    ),
    'maximum': QueryProfile(
        key='maximum',
        label=_('Maximum speed'),
        description=_('Exact counts up to 1,000 matches; the rest shows as "1,000+".'),
        page_size=50,
        count_cap=1_000,
        literal_id_limit=1_000,
        api_limit=10,
    ),
}

DEFAULT_MODE = 'balanced'


def _mode_or_default(mode, profiles):
    return mode if mode in profiles else DEFAULT_MODE


def _stored_modes():
    # Imported here so this module stays importable without a configured app.
    from .models import PerformanceSettings

    stored = PerformanceSettings.load()
    return stored.import_mode, stored.query_mode


def import_profile(mode=None):
    """The import profile for ``mode``, or for the mode saved in Settings."""
    if mode is None:
        mode = _stored_modes()[0]
    return IMPORT_PROFILES[_mode_or_default(mode, IMPORT_PROFILES)]


def query_profile(mode=None):
    """The query profile for ``mode``, or for the mode saved in Settings."""
    if mode is None:
        mode = _stored_modes()[1]
    return QUERY_PROFILES[_mode_or_default(mode, QUERY_PROFILES)]
