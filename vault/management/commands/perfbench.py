"""Reproducible benchmark suite for the CredVault ingestion and query paths.

Sub-commands (all print one JSON document so results can be archived)::

    perfbench generate  DIR --lines N [--files F] [--duplicate-rate R]
                            [--invalid-rate R] [--seed S]
    perfbench stages    PATH [--chunk-bytes ...]      # read/parse/validate/hash split
    perfbench queries   [--runs N] [--explain]        # real view query paths
    perfbench storage                                  # bytes per table/index/record
    perfbench concurrent PATH [--fast] [--query-workers N]
                                                       # searches during import

``generate`` writes deterministic synthetic files (same seed => same bytes) with
a skewed host distribution, a configurable duplicate rate and a configurable
share of malformed lines. Values are synthetic; nothing resembles a real
credential. Reports never print record contents.

The commands read and write only the database named by the ``POSTGRES_*``
environment variables; run them against a disposable database.
"""

import hashlib
import json
import os
import random
import resource
import threading
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from vault.utils import (
    MAX_SOURCE_LINE_BYTES, iter_bounded_source_lines, parse_line,
)


def synthetic_record(index, rng):
    """One synthetic valid line; hosts follow a Zipf-like skew."""
    host = int(rng.paretovariate(1.1)) % 200000
    kind = index % 7
    ident = f'user{index}@corp{host % 997}.example' if kind else f'user_{index}'
    ip = (
        f'10.{(index // 65025) % 250}.{(index // 250) % 250}.{index % 250}'
        if index % 3 else 'N/A'
    )
    return (
        f'https://host{host}.example.com/login/{index % 20011}'
        f'|{ident}|Secret-{index}-{rng.randrange(10**6)}|{ip}'
    )


def generate_files(directory, lines, files, duplicate_rate, invalid_rate, seed):
    """Write ``lines`` lines over ``files`` files; return a manifest dict."""
    os.makedirs(directory, exist_ok=True)
    rng = random.Random(seed)
    per_file = max(1, lines // files)
    written = 0
    unique_lines = []
    manifest = {'files': [], 'lines': 0, 'bytes': 0, 'expected_invalid': 0,
                'expected_duplicate_lines': 0, 'seed': seed}
    for file_index in range(files):
        count = per_file if file_index < files - 1 else lines - written
        path = os.path.join(directory, f'part-{file_index:05d}.txt')
        with open(path, 'w', encoding='utf-8', newline='\n') as out:
            for _ in range(count):
                roll = rng.random()
                if roll < invalid_rate:
                    out.write(f'not a record {written}\n')
                    manifest['expected_invalid'] += 1
                elif roll < invalid_rate + duplicate_rate and unique_lines:
                    out.write(rng.choice(unique_lines) + '\n')
                    manifest['expected_duplicate_lines'] += 1
                else:
                    line = synthetic_record(written, rng)
                    out.write(line + '\n')
                    if len(unique_lines) < 10000:
                        unique_lines.append(line)
                written += 1
        size = os.path.getsize(path)
        manifest['files'].append({'name': os.path.basename(path), 'lines': count,
                                  'bytes': size})
        manifest['bytes'] += size
    manifest['lines'] = written
    return manifest


def _rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def profile_stages(path, chunk_bytes):
    """Time each pipeline stage on one file, independently, in-process."""
    from vault.workers import _hash_line

    results = {}
    size = os.path.getsize(path)

    t0 = time.perf_counter()
    c0 = time.process_time()
    with open(path, 'rb') as handle:
        while handle.read(chunk_bytes):
            pass
    results['raw_read'] = (time.perf_counter() - t0, time.process_time() - c0)

    t0 = time.perf_counter()
    c0 = time.process_time()
    lines = []
    with open(path, 'rb', buffering=chunk_bytes) as handle:
        for line in iter_bounded_source_lines(handle):
            lines.append(line)
    results['bounded_line_split'] = (
        time.perf_counter() - t0, time.process_time() - c0)

    t0 = time.perf_counter()
    c0 = time.process_time()
    for line in lines:
        if isinstance(line, bytes):
            hashlib.sha256(line).hexdigest()
    results['sha256_per_line'] = (
        time.perf_counter() - t0, time.process_time() - c0)

    t0 = time.perf_counter()
    c0 = time.process_time()
    decoded = [line.decode('utf-8', errors='replace') for line in lines
               if isinstance(line, bytes)]
    results['decode'] = (time.perf_counter() - t0, time.process_time() - c0)

    t0 = time.perf_counter()
    c0 = time.process_time()
    parsed = valid = 0
    for text in decoded:
        parsed += 1
        if parse_line(text):
            valid += 1
    results['parse_validate_normalize'] = (
        time.perf_counter() - t0, time.process_time() - c0)

    total_wall = sum(v[0] for v in results.values()
                     if v is not results['raw_read'])
    out = {
        'file_bytes': size, 'lines': len(lines), 'valid_lines': valid,
        'peak_rss_mb': round(_rss_mb(), 1), 'chunk_bytes': chunk_bytes,
        'stages': {},
    }
    for name, (wall, cpu) in results.items():
        out['stages'][name] = {
            'wall_s': round(wall, 4), 'cpu_s': round(cpu, 4),
            'mb_per_s': round(size / 1e6 / wall, 1) if wall else None,
            'lines_per_s': round(len(lines) / wall) if wall else None,
            'cpu_us_per_line': round(cpu / max(len(lines), 1) * 1e6, 2),
            'share_of_in_process_pipeline_pct': (
                round(100 * wall / total_wall, 1)
                if name != 'raw_read' and total_wall else None),
        }
    return out


def percentile(sorted_values, fraction):
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1,
                int(round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


def _timed(callable_, runs):
    samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        callable_()
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    return {
        'runs': runs,
        'p50_ms': round(percentile(samples, 0.50), 1),
        'p95_ms': round(percentile(samples, 0.95), 1),
        'max_ms': round(samples[-1], 1),
    }


def query_suite(runs, explain):
    """Time the real view query paths against the current database."""
    from django.test import RequestFactory
    from vault import views
    from vault.counts import exact_count
    from vault.models import Account, Email, Url

    rf = RequestFactory()
    with connection.cursor() as cursor:
        cursor.execute('SELECT name FROM vault_email ORDER BY id OFFSET 12345 LIMIT 1')
        row = cursor.fetchone()
        email = row[0] if row else 'nobody@example.com'
        cursor.execute('SELECT name FROM vault_url ORDER BY id OFFSET 777 LIMIT 1')
        row = cursor.fetchone()
        url = row[0] if row else 'https://example.com'
    domain = url.split('/')[2] if '://' in url else url
    total_accounts = Account.objects.count()

    def page(view, params):
        def run():
            request = rf.get('/', params)
            request.user = type('U', (), {'is_authenticated': True,
                                           'is_staff': True,
                                           'is_superuser': True,
                                           'pk': 1})()
            from django.contrib.messages.storage.fallback import FallbackStorage
            request.session = {}
            setattr(request, '_messages', FallbackStorage(request))
            return view(request)
        return run

    cases = {
        'exact_email': page(views.search_records, {'q': email, 'field': 'email'}),
        'exact_domain': page(views.search_records, {'q': domain, 'field': 'domain'}),
        'substring_url_rare': page(views.advanced_search,
                                   {'url': 'host12345.example.com/login/1'}),
        'substring_url_common': page(views.advanced_search, {'url': 'example'}),
        'substring_keyword_common': page(views.advanced_search,
                                         {'keyword': 'corp'}),
        'deep_page_common_substring': page(views.advanced_search,
                                           {'url': 'example', 'page': 200}),
        'count_all_accounts': lambda: exact_count(Account.objects.all()),
    }
    report = {'accounts': total_accounts, 'cases': {}}
    for name, fn in cases.items():
        fn()  # warm
        report['cases'][name] = _timed(fn, runs)
        if name.startswith('substring') or name.startswith('exact'):
            # result-set size of the underlying filter, for context
            pass

    if explain:
        report['plans'] = {}
        with connection.cursor() as cursor:
            cursor.execute('SELECT id FROM vault_account ORDER BY id'
                           ' OFFSET %s LIMIT 1', [max(total_accounts // 2, 0)])
            row = cursor.fetchone()
            mid_id = row[0] if row else 0
        statements = {
            'count_all': ('SELECT count(*) FROM vault_account', None),
            # Django's icontains is UPPER(col) LIKE UPPER(pattern); that is
            # the expression the pg_trgm GIN index is built on.
            'substring_common_url_ids': (
                "SELECT id FROM vault_url WHERE upper(name) LIKE upper('%%example%%')",
                None),
            'substring_rare_url_ids': (
                "SELECT id FROM vault_url WHERE upper(name) LIKE"
                " upper('%%host12345.example.com/login/1%%')", None),
            'accounts_by_exact_email': (
                'SELECT a.id FROM vault_account a JOIN vault_email e ON e.id=a.email_id'
                ' WHERE lower(e.name)=lower(%s) ORDER BY a.created_at DESC, a.id LIMIT 100',
                [email]),
            'keyset_first_page': (
                'SELECT id FROM vault_account ORDER BY id LIMIT 100', None),
            'keyset_page_at_50pct': (
                'SELECT id FROM vault_account WHERE id > %s ORDER BY id LIMIT 100',
                [mid_id]),
            'offset_page_at_50pct': (
                'SELECT id FROM vault_account ORDER BY id OFFSET %s LIMIT 100',
                [max(total_accounts // 2, 0)]),
        }
        for name, (sql, params) in statements.items():
            with connection.cursor() as cursor:
                cursor.execute('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + sql, params)
                plan = cursor.fetchone()[0]
                plan = plan if isinstance(plan, list) else json.loads(plan)
            top = plan[0]
            report['plans'][name] = {
                'execution_ms': top['Execution Time'],
                'planning_ms': top['Planning Time'],
                'shared_hit': top['Plan'].get('Shared Hit Blocks'),
                'shared_read': top['Plan'].get('Shared Read Blocks'),
                'node': top['Plan']['Node Type'],
            }
    return report


def count_suite(patterns, caps, runs):
    """Exact versus capped search totals through the real search code path.

    For each url substring: exact count and its time, then the time of the
    bounded count for each cap, plus the whole advanced-search view with the
    cap off and on. Shows where a cap helps (broad matches) and that it costs
    nothing when matches are fewer than the cap.
    """
    from django.http import QueryDict
    from django.test import RequestFactory

    from vault import views
    from vault.counts import bounded_count

    def queryset(pattern):
        params = QueryDict('', mutable=True)
        params.update({'url': pattern, 'adv': '1'})
        return views.filter_accounts(params).values_list('pk', flat=True)

    def view_runner(pattern):
        run = _request_factory_view(views.advanced_search, {'url': pattern})
        return run

    report = {'accounts': 0, 'patterns': []}
    from vault.models import Account
    report['accounts'] = Account.objects.count()
    for pattern in patterns:
        qs = queryset(pattern)
        qs.order_by().count()  # warm
        exact = _timed(lambda: qs.order_by().count(), runs)
        matches = qs.order_by().count()
        entry = {'pattern': pattern, 'matches': matches, 'exact_count': exact,
                 'capped': {}}
        for cap in caps:
            entry['capped'][str(cap)] = _timed(
                lambda: bounded_count(qs, cap), runs)
        old = os.environ.get('VAULT_RESULT_COUNT_CAP')
        try:
            os.environ.pop('VAULT_RESULT_COUNT_CAP', None)
            entry['view_exact'] = _timed(view_runner(pattern), runs)
            os.environ['VAULT_RESULT_COUNT_CAP'] = str(caps[-1])
            entry['view_capped_%d' % caps[-1]] = _timed(view_runner(pattern), runs)
        finally:
            if old is None:
                os.environ.pop('VAULT_RESULT_COUNT_CAP', None)
            else:
                os.environ['VAULT_RESULT_COUNT_CAP'] = old
        report['patterns'].append(entry)
    return report


def _request_factory_view(view, params):
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.test import RequestFactory

    rf = RequestFactory()

    def run():
        request = rf.get('/', params)
        request.user = type('U', (), {'is_authenticated': True,
                                       'is_staff': True, 'is_superuser': True,
                                       'pk': 1})()
        request.session = {}
        setattr(request, '_messages', FallbackStorage(request))
        response = view(request)
        if hasattr(response, 'render'):
            response.render()
        return response
    return run


def concurrent_suite(path, fast, query_workers, baseline_runs, think_s):
    """Run searches through the real views while a durable import runs.

    Reports per-query latency before the import (idle) and during it, plus
    import throughput, WAL volume and database growth. Run it on a copy of a
    populated database (``CREATE DATABASE x TEMPLATE y``) so each variant
    starts from identical data.
    """
    from django.core.management import call_command
    from django.db import close_old_connections, connections
    from vault import views

    with connection.cursor() as cursor:
        cursor.execute('SELECT name FROM vault_email ORDER BY id OFFSET 12345 LIMIT 1')
        email = cursor.fetchone()[0]
        cursor.execute('SELECT count(*) FROM vault_account')
        accounts_before = cursor.fetchone()[0]
        cursor.execute('SELECT pg_database_size(current_database()), wal_bytes FROM pg_stat_wal')
        size_before, wal_before = cursor.fetchone()

    cases = {
        'exact_email': _request_factory_view(
            views.search_records, {'q': email, 'field': 'email'}),
        'exact_domain_broad': _request_factory_view(
            views.search_records, {'q': 'host1.example.com', 'field': 'domain'}),
        'exact_domain_rare': _request_factory_view(
            views.search_records, {'q': 'host12345.example.com', 'field': 'domain'}),
        'substring_url_rare': _request_factory_view(
            views.advanced_search, {'url': 'host12345.example.com/login/1'}),
        'substring_url_broad': _request_factory_view(
            views.advanced_search, {'url': 'host1.'}),
        'substring_keyword': _request_factory_view(
            views.advanced_search, {'keyword': 'corp12'}),
        'cursor_page_broad': _request_factory_view(
            views.search_records, {'q': 'host1.example.com', 'field': 'domain',
                                   'sort': 'id', 'after': 0}),
    }

    def sample(sink, stop=None, runs=None):
        names = list(cases)
        done = 0
        while True:
            for name in names:
                t0 = time.perf_counter()
                cases[name]()
                sink.setdefault(name, []).append(
                    (time.perf_counter() - t0) * 1000)
                if stop is not None and stop.is_set():
                    return
                time.sleep(think_s)
            done += 1
            if runs is not None and done >= runs:
                return

    for fn in cases.values():
        fn()  # warm caches / plans
    baseline = {}
    sample(baseline, runs=baseline_runs)

    stop = threading.Event()
    sinks = [dict() for _ in range(query_workers)]
    errors = []

    def query_worker(sink):
        try:
            sample(sink, stop=stop)
        except Exception as exc:  # pragma: no cover - reported, not hidden
            errors.append(f'{type(exc).__name__}: {str(exc)[:400]}')
        finally:
            connections.close_all()

    threads = [threading.Thread(target=query_worker, args=(sink,))
               for sink in sinks]
    for thread in threads:
        thread.start()
    started = time.perf_counter()
    import_error = None
    try:
        args = [path] + (['--fast'] if fast else [])
        call_command('importfolder', *args, stdout=open(os.devnull, 'w'))
    except Exception as exc:  # pragma: no cover
        import_error = type(exc).__name__
    elapsed = time.perf_counter() - started
    stop.set()
    for thread in threads:
        thread.join()
    close_old_connections()

    during = {}
    for sink in sinks:
        for name, values in sink.items():
            during.setdefault(name, []).extend(values)

    with connection.cursor() as cursor:
        cursor.execute('SELECT count(*) FROM vault_account')
        accounts_after = cursor.fetchone()[0]
        cursor.execute('SELECT pg_database_size(current_database()), wal_bytes FROM pg_stat_wal')
        size_after, wal_after = cursor.fetchone()

    def summarize(values):
        values = sorted(values)
        return {
            'n': len(values),
            'p50_ms': round(percentile(values, 0.50), 1),
            'p95_ms': round(percentile(values, 0.95), 1),
            'p99_ms': round(percentile(values, 0.99), 1),
            'max_ms': round(values[-1], 1),
        } if values else {'n': 0}

    imported = accounts_after - accounts_before
    return {
        'fast_mode': fast, 'query_workers': query_workers,
        'import_seconds': round(elapsed, 1),
        'accounts_before': accounts_before, 'accounts_added': imported,
        'accounts_per_second': round(imported / elapsed, 1),
        'database_growth_bytes': size_after - size_before,
        'wal_bytes': wal_after - wal_before,
        'import_error': import_error, 'query_errors': errors,
        'baseline': {k: summarize(v) for k, v in baseline.items()},
        'during_import': {k: summarize(v) for k, v in during.items()},
    }


STORAGE_SQL = """
SELECT c.relname,
       pg_total_relation_size(c.oid) AS total_bytes,
       pg_relation_size(c.oid) AS heap_bytes,
       pg_indexes_size(c.oid) AS index_bytes,
       c.reltuples::bigint AS est_rows
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname LIKE 'vault_%%'
 ORDER BY total_bytes DESC
"""

INDEX_SQL = """
SELECT s.relname AS table_name, s.indexrelname AS index_name,
       pg_relation_size(s.indexrelid) AS bytes, s.idx_scan AS scans
  FROM pg_stat_user_indexes s
 WHERE s.relname LIKE 'vault_%%'
 ORDER BY bytes DESC
"""


def storage_report():
    with connection.cursor() as cursor:
        cursor.execute(STORAGE_SQL)
        tables = [dict(zip(['table', 'total_bytes', 'heap_bytes', 'index_bytes',
                            'est_rows'], row)) for row in cursor.fetchall()]
        cursor.execute(INDEX_SQL)
        indexes = [dict(zip(['table', 'index', 'bytes', 'scans'], row))
                   for row in cursor.fetchall()]
        cursor.execute('SELECT count(*) FROM vault_account')
        accounts = cursor.fetchone()[0]
        cursor.execute('SELECT pg_database_size(current_database())')
        db_bytes = cursor.fetchone()[0]
    total = sum(t['total_bytes'] for t in tables)
    return {
        'accounts': accounts, 'database_bytes': db_bytes,
        'vault_tables_total_bytes': total,
        'bytes_per_account': round(total / accounts, 1) if accounts else None,
        'tables': tables, 'indexes': indexes,
    }


class Command(BaseCommand):
    help = 'Reproducible ingestion/query benchmark suite (prints JSON).'

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest='action', required=True)
        gen = sub.add_parser('generate')
        gen.add_argument('directory')
        gen.add_argument('--lines', type=int, required=True)
        gen.add_argument('--files', type=int, default=1)
        gen.add_argument('--duplicate-rate', type=float, default=0.0)
        gen.add_argument('--invalid-rate', type=float, default=0.0)
        gen.add_argument('--seed', type=int, default=1)
        stages = sub.add_parser('stages')
        stages.add_argument('path')
        stages.add_argument('--chunk-bytes', type=int, nargs='+',
                            default=[65536])
        queries = sub.add_parser('queries')
        queries.add_argument('--runs', type=int, default=7)
        queries.add_argument('--explain', action='store_true')
        counts = sub.add_parser('counts')
        counts.add_argument('--patterns', nargs='+', default=[
            'example', 'host1.example', 'login/1', 'login/12', 'login/123',
            'login/1234', 'login/12345', 'host12345.example.com/login/1'])
        counts.add_argument('--caps', type=int, nargs='+',
                            default=[10000, 100000])
        counts.add_argument('--runs', type=int, default=5)
        sub.add_parser('storage')
        conc = sub.add_parser('concurrent')
        conc.add_argument('path')
        conc.add_argument('--fast', action='store_true')
        conc.add_argument('--query-workers', type=int, default=2)
        conc.add_argument('--baseline-runs', type=int, default=10)
        conc.add_argument('--think-ms', type=int, default=50)

    def handle(self, *args, **options):
        action = options['action']
        if action == 'generate':
            if not 0 <= options['duplicate_rate'] + options['invalid_rate'] < 1:
                raise CommandError('duplicate + invalid rate must be < 1')
            result = generate_files(
                options['directory'], options['lines'], options['files'],
                options['duplicate_rate'], options['invalid_rate'],
                options['seed'])
        elif action == 'stages':
            result = [profile_stages(options['path'], size)
                      for size in options['chunk_bytes']]
        elif action == 'queries':
            result = query_suite(options['runs'], options['explain'])
        elif action == 'counts':
            result = count_suite(options['patterns'], options['caps'],
                                 options['runs'])
        elif action == 'concurrent':
            result = concurrent_suite(
                options['path'], options['fast'], options['query_workers'],
                options['baseline_runs'], options['think_ms'] / 1000.0)
        else:
            result = storage_report()
        self.stdout.write(json.dumps(result, indent=2, default=str))
