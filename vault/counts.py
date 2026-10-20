"""Counts and display formatting for the vault's paginated views.

Default contract (unchanged): a search reports the real number of matches and
the paginator reaches every page; nothing stops at a row threshold. PostgreSQL
must visit every matching index entry for an exact ``COUNT(*)``, and measured
on this code base that count -- not the page query -- is the whole cost of a
broad search (2 ms to fetch a page versus 1-2 s to count 7.4 M accounts, see
``docs/scalability/BENCHMARK_RESULTS.md``). It grows linearly with the data set.

Two scale controls exist, both explicit and visible to the user:

``VAULT_RESULT_COUNT_CAP`` (default ``0`` = exact, no cap)
    When set to N > 0, a search total is computed by reading at most N + 1
    matching rows. A search with more than N matches is labelled ``N+`` and its
    page numbers stop at N rows; a search with N or fewer matches is still
    exact. Exports are unaffected and stream every match, and the cursor mode
    (``?sort=id&after=``) walks every match regardless of the cap.

``VAULT_TOTAL_COUNT_TTL`` (default ``60`` seconds, ``0`` = always live)
    The whole-table totals on the dashboard, Records and References pages are
    exact when the table is small. For a table of at least
    ``VAULT_TOTAL_COUNT_MIN_ROWS`` (default 1,000,000) estimated rows the
    exact count is cached for the TTL, so a page load does not rescan the
    table; one process recounts while the others keep serving the previous
    value. Adding or deleting a record through the UI refreshes the cache.
    Imports run in another process and become visible within the TTL.

Neither control changes what is stored or exported.
"""

import os
import time

from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import connection
from django.utils.functional import cached_property

DEFAULT_TOTAL_TTL_SECONDS = 60
DEFAULT_TOTAL_MIN_ROWS = 1_000_000
MAX_RESULT_COUNT_CAP = 1_000_000_000
_TOTAL_KEY = 'vault:table-total:%s'
_LOCK_KEY = 'vault:table-total-lock:%s'
_LOCK_SECONDS = 120


def _int_env(name, default, minimum=0, maximum=None):
    raw = os.environ.get(name, '')
    try:
        value = int(raw.strip()) if raw.strip() else default
    except ValueError:
        return default
    value = max(minimum, value)
    return value if maximum is None else min(maximum, value)


def result_count_cap():
    """Search-total cap from the environment; 0 means exact (default)."""
    return _int_env('VAULT_RESULT_COUNT_CAP', 0, 0, MAX_RESULT_COUNT_CAP)


def total_count_ttl():
    return _int_env('VAULT_TOTAL_COUNT_TTL', DEFAULT_TOTAL_TTL_SECONDS, 0, 86400)


def total_count_min_rows():
    return _int_env('VAULT_TOTAL_COUNT_MIN_ROWS', DEFAULT_TOTAL_MIN_ROWS, 0)


def exact_count(queryset):
    """Return the exact number of rows represented by a queryset."""
    return queryset.order_by().count()


def bounded_count(queryset, cap):
    """Return ``(count, capped)`` reading at most ``cap + 1`` matching rows.

    ``count`` equals the true number of rows when that is at most ``cap`` and
    is ``cap`` otherwise, in which case ``capped`` is True.
    """
    limited = queryset.order_by()[:cap + 1]
    seen = limited.count()
    if seen > cap:
        return cap, True
    return seen, False


def count_label(number, capped=False):
    """Format a count with thousands separators; ``12,345+`` when capped."""
    label = f'{number:,}'
    return f'{label}+' if capped else label


class CappedPaginator(Paginator):
    """Paginator whose total reads at most ``cap + 1`` rows.

    ``count`` never exceeds ``cap``; ``capped`` tells the template that more
    rows exist. Pages beyond the cap are not reachable.
    """

    def __init__(self, object_list, per_page, cap, **kwargs):
        super().__init__(object_list, per_page, **kwargs)
        self.cap = cap
        self.capped = False

    @cached_property
    def count(self):
        value, self.capped = bounded_count(self.object_list, self.cap)
        return value


class CachedTotalPaginator(Paginator):
    """Paginator over a whole table whose total comes from ``table_total``."""

    def __init__(self, object_list, per_page, model, **kwargs):
        super().__init__(object_list, per_page, **kwargs)
        self._model = model

    @cached_property
    def count(self):
        return table_total(self._model)


def make_search_paginator(object_list, per_page):
    """Exact paginator by default, capped when ``VAULT_RESULT_COUNT_CAP`` is set."""
    cap = result_count_cap()
    if cap > 0:
        return CappedPaginator(object_list, per_page, cap)
    return Paginator(object_list, per_page)


def paginator_total(paginator):
    """``(total, capped)`` for a numbered page's paginator."""
    return paginator.count, bool(getattr(paginator, 'capped', False))


def estimated_rows(model):
    """Planner row estimate (``pg_class.reltuples``), or None when unknown."""
    if connection.vendor != 'postgresql':
        return None
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass(%s)',
            [connection.ops.quote_name(model._meta.db_table)],
        )
        row = cursor.fetchone()
    if not row or row[0] is None or row[0] < 0:
        return None  # never analyzed: unknown, so count exactly
    return int(row[0])


def table_total(model):
    """Exact row count of a whole table, cached for large tables (see module doc)."""
    ttl = total_count_ttl()
    live = model.objects.order_by().count
    if ttl <= 0:
        return live()
    estimate = estimated_rows(model)
    if estimate is None or estimate < total_count_min_rows():
        return live()

    name = model._meta.db_table
    key = _TOTAL_KEY % name
    now = time.time()
    entry = cache.get(key)
    if entry is not None and now - entry[1] < ttl:
        return entry[0]
    # Only one caller recounts; the rest serve the previous value if any.
    if cache.add(_LOCK_KEY % name, 1, _LOCK_SECONDS) or entry is None:
        try:
            value = live()
            cache.set(key, (value, time.time()), max(ttl * 10, 600))
            return value
        finally:
            cache.delete(_LOCK_KEY % name)
    return entry[0]


def invalidate_table_totals(*models):
    """Drop cached totals (all tables when no model is given)."""
    if not models:
        from .models import Account, Email, Ip, Pass, Url, User
        models = (Account, Email, Ip, Pass, Url, User)
    for model in models:
        cache.delete(_TOTAL_KEY % model._meta.db_table)
