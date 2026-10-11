"""
Database helpers that keep the workspace responsive when a table holds
billions of rows.

The two rules behind them:

* Never count a large table with ``COUNT(*)`` on a page load. On PostgreSQL an
  unfiltered count reads every row; the planner's own row estimate
  (``pg_class.reltuples``, refreshed by autovacuum and ``ANALYZE``) answers the
  same question in microseconds and is labelled as approximate.
* A filtered count only has to reach the page size, not the total. Counting
  the first ``cap`` matching rows bounds the work; past the cap the page shows
  "more than N" and asks the user to narrow the search.
"""

from django.db import DEFAULT_DB_ALIAS, connections, router

#: Largest exact count taken when no planner estimate is available.
EXACT_COUNT_LIMIT = 1_000_000


def is_postgresql(using=None):
    return connections[using or DEFAULT_DB_ALIAS].vendor == 'postgresql'


def capped_count(queryset, cap):
    """
    Return ``(count, capped)``: the exact number of rows when it is below
    ``cap``, otherwise ``cap`` with ``capped=True``.

    The query counts at most ``cap`` rows, so its cost is bounded however many
    rows match.
    """
    total = queryset.order_by().values('pk')[:cap].count()
    return total, total >= cap


def table_count(model, using=None):
    """
    Return ``(count, approximate)`` for a whole table.

    On PostgreSQL the planner's estimate is used once the table has been
    analyzed. A table that has never been analyzed (``reltuples = -1``) is
    counted exactly, up to ``EXACT_COUNT_LIMIT``.
    """
    alias = using or router.db_for_read(model)
    connection = connections[alias]
    if connection.vendor != 'postgresql':
        # SQLite has no planner estimate; a plain COUNT(*) is its fastest exact count.
        return model.objects.using(alias).count(), False

    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass(%s)',
            [model._meta.db_table],
        )
        row = cursor.fetchone()
    if row and row[0] is not None and row[0] >= 0:
        return int(row[0]), True

    count, _capped = capped_count(
        model.objects.using(alias).all(), EXACT_COUNT_LIMIT
    )
    return count, False


def analyze_tables(models, using=None):
    """Refresh planner statistics after a bulk load (PostgreSQL only)."""
    alias = using or DEFAULT_DB_ALIAS
    connection = connections[alias]
    if connection.vendor != 'postgresql':
        return
    tables = ', '.join(model._meta.db_table for model in models)
    with connection.cursor() as cursor:
        cursor.execute(f'ANALYZE {tables}')


def index_names(using=None):
    """Names of the indexes present on the vault tables (PostgreSQL only)."""
    alias = using or DEFAULT_DB_ALIAS
    connection = connections[alias]
    if connection.vendor != 'postgresql':
        return set()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename LIKE 'vault_%%'"
        )
        return {row[0] for row in cursor.fetchall()}


def text_collation_is_c(table, column='name', using=None):
    """
    True when ``table.column`` sorts by byte order (collation C or POSIX).

    Only then does a plain B-tree on ``lower(column)`` serve ``LIKE 'x%'``
    prefix searches, so the extra ``text_pattern_ops`` index is not needed.
    PostgreSQL only; returns False on other databases.
    """
    alias = using or DEFAULT_DB_ALIAS
    connection = connections[alias]
    if connection.vendor != 'postgresql':
        return False
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT CASE WHEN c.collname IS NULL OR c.collname = 'default' "
            "THEN d.datlocprovider::text ELSE c.collprovider::text END, "
            "CASE WHEN c.collname IS NULL OR c.collname = 'default' "
            "THEN d.datcollate ELSE c.collcollate END "
            'FROM pg_attribute a '
            'LEFT JOIN pg_collation c ON c.oid = a.attcollation '
            'JOIN pg_database d ON d.datname = current_database() '
            'WHERE a.attrelid = %s::regclass AND a.attname = %s',
            [table, column],
        )
        row = cursor.fetchone()
    # Only libc's C/POSIX collations sort by byte order. ICU and other
    # providers (even when datcollate reads 'C') need the prefix index.
    return bool(row) and row[0] == 'c' and row[1] in ('C', 'POSIX')
