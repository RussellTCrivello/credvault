"""
Copy an existing SQLite database into the PostgreSQL database in settings.

    python manage.py migrate                       # create the schema first
    python manage.py copy_from_sqlite /path/to/db.sqlite3

What it does, all inside one transaction (any failure leaves the target
unchanged):

  * copies every application table and the Django auth tables, so the admin
    accounts, groups, permissions, sessions and admin history come across;
  * keeps the original primary keys, so foreign keys and fingerprints stay
    valid, and then resets each id sequence past the largest copied id;
  * converts SQLite's storage format to PostgreSQL types (0/1 to boolean,
    stored text to timestamps and dates);
  * checks that every table has the same row count in both databases, and
    that every stored fingerprint matches the one recomputed from its
    relations.

The target must be empty for application data. The Django migration history
(django_migrations) is not copied: the schema comes from `migrate`.
"""

import datetime as dt
import os
import sqlite3

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from vault.importer import _FINGERPRINT_SQL

# Parents before children, so foreign keys can be checked in this order.
COPY_ORDER = [
    'django_content_type',
    'auth_permission',
    'auth_group',
    'auth_group_permissions',
    'auth_user',
    'auth_user_groups',
    'auth_user_user_permissions',
    'django_admin_log',
    'django_session',
    'vault_url',
    'vault_email',
    'vault_user',
    'vault_pass',
    'vault_ip',
    'vault_account',
]

# Created automatically by `migrate` (one row per model permission and content
# type). The copy replaces them with the source rows, keeping the source ids.
AUTO_CREATED = ('django_content_type', 'auth_permission')

# Tables that must be empty in the target before the copy, so nothing is lost
# or merged silently.
MUST_BE_EMPTY = [
    t for t in COPY_ORDER if t not in AUTO_CREATED
]

UTC = dt.timezone.utc


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _to_datetime(value, aware):
    if value is None or isinstance(value, dt.datetime):
        return value
    parsed = dt.datetime.fromisoformat(str(value))
    if aware and parsed.tzinfo is None:
        # Django stores aware datetimes as naive UTC text in SQLite.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def converter_for(data_type):
    """Return a function that turns one SQLite value into a PostgreSQL value."""
    if data_type == 'boolean':
        return lambda v: None if v is None else bool(v)
    if data_type == 'timestamp with time zone':
        return lambda v: _to_datetime(v, aware=True)
    if data_type == 'timestamp without time zone':
        return lambda v: _to_datetime(v, aware=False)
    if data_type == 'date':
        return lambda v: None if v is None else dt.date.fromisoformat(str(v)[:10])
    return lambda v: v


class Command(BaseCommand):
    help = (
        'Copy all data from a SQLite database (the old db.sqlite3) into the '
        'PostgreSQL database configured in settings. Run `migrate` first.'
    )

    def add_arguments(self, parser):
        parser.add_argument('sqlite_path', help='Path to the SQLite file.')
        parser.add_argument(
            '--progress-every', type=int, default=1000000,
            help='Print progress after this many rows (default: 1,000,000).',
        )

    def handle(self, *args, **options):
        path = options['sqlite_path']
        if not os.path.isfile(path):
            raise CommandError(f'SQLite file not found: {path}')

        source = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            source_tables = {
                row[0] for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            with connection.cursor() as cursor:
                target_tables = set(
                    connection.introspection.table_names(cursor)
                )
            missing = [
                t for t in COPY_ORDER
                if t in source_tables and t not in target_tables
            ]
            if missing:
                raise CommandError(
                    'The target database has no schema for: '
                    + ', '.join(missing) + '. Run `migrate` first.'
                )

            tables = [t for t in COPY_ORDER if t in source_tables]
            skipped = [t for t in COPY_ORDER if t not in source_tables]
            if skipped:
                self.stdout.write(self.style.WARNING(
                    'Not in the SQLite file, skipped: ' + ', '.join(skipped)
                ))

            with transaction.atomic():
                self._require_empty()
                self._clear_auto_created()
                copied = {}
                with connection.cursor() as cursor:
                    for table in tables:
                        copied[table] = self._copy_table(
                            source, cursor, table, options['progress_every'],
                        )
                    self._reset_sequences(cursor, tables)
                self._verify(source, tables)
                with connection.cursor() as cursor:
                    cursor.execute('ANALYZE')
        finally:
            source.close()

        for table in tables:
            self.stdout.write(f'  {table}: {copied[table]:,} rows')
        self.stdout.write(self.style.SUCCESS(
            'Copy complete. Row counts match and every fingerprint verified.'
        ))

    # --- steps -------------------------------------------------------------

    def _require_empty(self):
        with connection.cursor() as cursor:
            for table in MUST_BE_EMPTY:
                cursor.execute(f'SELECT EXISTS (SELECT 1 FROM {_quote(table)})')
                if cursor.fetchone()[0]:
                    raise CommandError(
                        f'{table} already contains rows. Copy into an empty '
                        'database (create a new one and run `migrate`).'
                    )

    def _clear_auto_created(self):
        with connection.cursor() as cursor:
            cursor.execute(
                'TRUNCATE ' + ', '.join(_quote(t) for t in AUTO_CREATED)
                + ' CASCADE'
            )

    def _target_columns(self, cursor, table):
        cursor.execute(
            'SELECT column_name, data_type FROM information_schema.columns'
            ' WHERE table_schema = current_schema() AND table_name = %s',
            [table],
        )
        return dict(cursor.fetchall())

    def _required_columns(self, cursor, table):
        """Target columns that are NOT NULL with no default.

        A source row cannot be copied into one of these when the SQLite
        file has no column of the same name, so they must be present in
        the source; everything else (nullable columns, columns with a
        default such as the id sequences) may be absent.
        """
        cursor.execute(
            'SELECT column_name FROM information_schema.columns'
            ' WHERE table_schema = current_schema() AND table_name = %s'
            " AND is_nullable = 'NO' AND column_default IS NULL",
            [table],
        )
        return {row[0] for row in cursor.fetchall()}

    def _copy_table(self, source, cursor, table, progress_every):
        source_cols = [
            row[1] for row in source.execute(
                f'PRAGMA table_info({_quote(table)})'
            )
        ]
        target = self._target_columns(cursor, table)
        missing_required = sorted(
            c for c in self._required_columns(cursor, table)
            if c not in source_cols
        )
        if missing_required:
            raise CommandError(
                f'{table}: the SQLite file has no '
                f'{", ".join(missing_required)} column(s), which the '
                'current schema requires. This command supports the '
                "workspace's SQLite schema from the revision that added "
                'record fingerprints; the file appears to predate it. '
                'Nothing was saved.'
            )
        columns = [c for c in source_cols if c in target]
        ignored = [c for c in source_cols if c not in target]
        if ignored:
            self.stdout.write(self.style.WARNING(
                f'{table}: columns not in the new schema, not copied: '
                + ', '.join(ignored)
            ))
        converters = [converter_for(target[c]) for c in columns]

        select = (
            'SELECT ' + ', '.join(_quote(c) for c in columns)
            + f' FROM {_quote(table)}'
        )
        copy_sql = (
            f'COPY {_quote(table)} ({", ".join(_quote(c) for c in columns)})'
            ' FROM STDIN'
        )
        self.stdout.write(f'Copying {table}...')
        count = 0
        with cursor.copy(copy_sql) as copy:
            for row in source.execute(select):
                copy.write_row([
                    convert(value) for convert, value in zip(converters, row)
                ])
                count += 1
                if progress_every and count % progress_every == 0:
                    self.stdout.write(f'  {table}: {count:,} rows...')
        return count

    def _reset_sequences(self, cursor, tables):
        for table in tables:
            cursor.execute(
                'SELECT EXISTS (SELECT 1 FROM information_schema.columns'
                ' WHERE table_schema = current_schema()'
                ' AND table_name = %s AND column_name = %s)',
                [table, 'id'],
            )
            if not cursor.fetchone()[0]:
                continue
            cursor.execute(
                f'SELECT setval(pg_get_serial_sequence(%s, %s),'
                f' COALESCE((SELECT MAX(id) FROM {_quote(table)}), 1),'
                f' (SELECT MAX(id) IS NOT NULL FROM {_quote(table)}))',
                [table, 'id'],
            )

    def _verify(self, source, tables):
        with connection.cursor() as cursor:
            for table in tables:
                cursor.execute(f'SELECT COUNT(*) FROM {_quote(table)}')
                target_count = cursor.fetchone()[0]
                source_count = source.execute(
                    f'SELECT COUNT(*) FROM {_quote(table)}'
                ).fetchone()[0]
                if target_count != source_count:
                    raise CommandError(
                        f'{table}: {source_count} rows in SQLite but '
                        f'{target_count} in PostgreSQL. Nothing was saved.'
                    )
            if 'vault_account' in tables:
                cursor.execute(
                    'SELECT COUNT(*) FROM vault_account AS k'
                    f' WHERE k.fingerprint IS DISTINCT FROM ({_FINGERPRINT_SQL})'
                )
                bad = cursor.fetchone()[0]
                if bad:
                    raise CommandError(
                        f'{bad} records have a fingerprint that does not match '
                        'their relations. Nothing was saved.'
                    )
