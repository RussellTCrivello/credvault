"""Read-only PostgreSQL settings advisory for imports.

Field evidence (docs/scalability/BENCHMARK_RESULTS.md section 24): the same
18.2 M-line import ran at about 6,900 lines/s on PostgreSQL's stock settings
and about 12,000-13,000 lines/s after ``shared_buffers`` and the WAL settings
were raised, yet nothing in the application showed that the server was
running on defaults. This module reports the settings that matter at the
start of an import and warns when they are at or below the stock defaults
that were measured to be slow.

It never changes a setting, never opens a second connection, and never
raises into an import. Only server configuration values are read (no data
and no credentials). It logs once per process through the existing
``vault.import.metrics`` logger. Set ``VAULT_PG_ADVISORY=0`` to silence it.
"""

import logging
import os
import threading

logger = logging.getLogger('vault.import.metrics')

#: Settings reported at INFO.
REPORTED = (
    'shared_buffers', 'effective_cache_size', 'max_wal_size',
    'checkpoint_timeout', 'wal_buffers', 'maintenance_work_mem',
    'work_mem', 'synchronous_commit', 'wal_compression',
)

#: Stock PostgreSQL defaults (bytes / seconds). A value at or below these is
#: the configuration that was measured to be slow, so only that is warned about.
STOCK_AT_OR_BELOW = {
    'shared_buffers': 128 * 1024 ** 2,
    'max_wal_size': 1024 ** 3,
}
# checkpoint_timeout is reported but deliberately not warned about: a
# 30-minute value had no measurable effect in the sandbox experiments, and the
# field run changed it together with the memory/WAL settings.


_UNIT_FACTORS = {
    'B': 1, 'kB': 1024, '8kB': 8192, 'MB': 1024 ** 2, 'GB': 1024 ** 3,
    'TB': 1024 ** 4, 'ms': 0.001, 's': 1, 'min': 60, 'h': 3600, 'd': 86400,
}

_lock = threading.Lock()
_reported = False


def advisory_enabled():
    return os.environ.get('VAULT_PG_ADVISORY', '1').strip().lower() not in (
        '0', 'false', 'off', 'no',
    )


def read_pg_settings(connection=None):
    """Return ``{name: value}`` (bytes/seconds when the setting has a unit).

    Returns ``{}`` for a non-PostgreSQL backend or on any error.
    """
    try:
        if connection is None:
            from django.db import connection as default_connection
            connection = default_connection
        if connection.vendor != 'postgresql':
            return {}
        names = REPORTED + ('server_version',)
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT name, setting, unit FROM pg_settings '
                'WHERE name = ANY(%s)', [list(names)],
            )
            rows = cursor.fetchall()
    except Exception:  # advisory only: never disturb an import
        return {}

    settings = {}
    for name, setting, unit in rows:
        factor = _UNIT_FACTORS.get(unit) if unit else None
        if factor is not None:
            try:
                value = float(setting) * factor
                settings[name] = int(value) if value == int(value) else value
                continue
            except ValueError:
                pass
        settings[name] = setting
    return settings


def _format_bytes(value):
    for unit, size in (('GB', 1024 ** 3), ('MB', 1024 ** 2), ('kB', 1024)):
        if value >= size:
            number = value / size
            text = f'{number:g}'
            return f'{text} {unit}'
    return f'{value} B'


def _format_value(name, value):
    if not isinstance(value, (int, float)):
        return str(value)
    if value < 0:
        return 'auto'
    if name == 'checkpoint_timeout':
        return f'{value / 60:g} min' if value >= 60 else f'{value:g} s'
    return _format_bytes(value)


def describe(settings):
    """One-line snapshot of the reported settings."""
    parts = [
        f'{name}={_format_value(name, settings[name])}'
        for name in REPORTED if name in settings
    ]
    version = settings.get('server_version')
    prefix = f'postgresql {version} ' if version else 'postgresql '
    return prefix + ' '.join(parts)


def advisories(settings):
    """Warnings for settings at or below the measured-slow stock defaults."""
    at_stock = []
    for name, limit in STOCK_AT_OR_BELOW.items():
        value = settings.get(name)
        if isinstance(value, (int, float)) and value <= limit:
            at_stock.append(f'{name}={_format_value(name, value)}')
    if not at_stock:
        return []
    return [(
        'PostgreSQL is at or below its stock defaults for ' +
        ', '.join(at_stock) + '. In one measured field run (18.2 M lines, '
        'Windows, 32 GB RAM) raising shared_buffers to 4 GB and max_wal_size to '
        '32 GB, together with other settings, made an import about 1.7x '
        'faster; which setting mattered was not isolated and the benefit on '
        'other machines is not measured. Size these to this machine (see "Tuning for a fast machine" in docs/README.md). '
        'Set VAULT_PG_ADVISORY=0 to silence this message.'
    )]


def report_once(connection=None):
    """Log the settings snapshot and any warning, once per process."""
    global _reported
    if not advisory_enabled():
        return
    with _lock:
        if _reported:
            return
        _reported = True
    try:
        settings = read_pg_settings(connection)
        if not settings:
            return
        logger.info('pg_settings %s', describe(settings))
        for message in advisories(settings):
            logger.warning('pg_advisory %s', message)
    except Exception:
        return


def reset_for_tests():
    global _reported
    with _lock:
        _reported = False
