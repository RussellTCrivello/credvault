#!/usr/bin/env python
"""
Scale check for the two things this project has to be good at:

  1. ingesting a folder of very large text files (millions of lines)
  2. answering an exact domain / URL / email / username / password / IP
     query in well under a second, and exporting *every* match

It writes to its own SQLite file (``--db``), so your workspace database is
never touched.

    python scripts/scale_check.py --records 200000
    python scripts/scale_check.py --records 2000000 --fast
    python scripts/scale_check.py --db /tmp/vault-bench.sqlite3 --records 5000000

Use --fresh to rebuild the benchmark database instead of reusing it.
"""

import argparse
import os
import random
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'admin_LogIntel.settings')

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.core.management import call_command  # noqa: E402


def human(number):
    for unit, factor in (('B', 1_000_000_000), ('M', 1_000_000),
                         ('K', 1_000)):
        if number >= factor:
            return f'{number / factor:.1f}{unit}'
    return str(number)


def generate_folder(folder, records, files, domains, hot_domain_share,
                    hot_password_share, seed=1234):
    """Write `records` lines spread over `files` files inside `folder`."""
    rng = random.Random(seed)
    handles = []
    os.makedirs(folder, exist_ok=True)
    for index in range(files):
        path = os.path.join(folder, f'part-{index:03d}.txt')
        handles.append((path, open(path, 'w', encoding='utf-8')))
    # A decoy that must be ignored by the folder scan.
    with open(os.path.join(folder, 'notes.md'), 'w', encoding='utf-8') as f:
        f.write('not an import candidate\n')

    for number in range(records):
        if rng.random() < hot_domain_share:
            domain = 'gmail.com'
        else:
            domain = f'{rng.choice(domains)}{rng.randrange(len(domains))}.com'
        url = f'https://{domain}/login'
        identifier = (f'user{number}@{domain}' if rng.random() < 0.6
                      else f'user{number}')
        password = ('ReusedPass123' if rng.random() < hot_password_share
                    else f'secret-{rng.randrange(10 ** 6):07d}')
        ip = ('N/A' if rng.random() < 0.25
              else f'10.{rng.randrange(256)}.{rng.randrange(256)}.'
                   f'{rng.randrange(256)}')
        line = f'{url}|{identifier}|{password}|{ip}\n'
        handles[number % files][1].write(line)
        if number % 50_000 == 0:
            print(f'  generated {human(number)}/{human(records)} lines',
                  end='\r', flush=True)

    for _path, handle in handles:
        handle.close()
    print(f'  generated {human(records)} lines in {files} file(s)'
          + ' ' * 30 + '\n')
    sample = os.path.join(folder, 'part-000.txt')
    with open(sample, encoding='utf-8') as handle:
        return sample, handle.readline().strip()


def timed(label, function, expect=None):
    started = time.perf_counter()
    result = function()
    elapsed = (time.perf_counter() - started) * 1000
    flag = ''
    if expect is not None:
        flag = '  ' + (
            'ok' if result == expect
            else f'MISMATCH (got {result}, want {expect})'
        )
    print(f'{label:<52}{elapsed:>9.1f} ms{flag}')
    return result, elapsed


def query_plan(sql, params):
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute('EXPLAIN QUERY PLAN ' + sql, params)
        return ' | '.join(str(row[3]) for row in cursor.fetchall())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records', type=int, default=200_000)
    parser.add_argument('--files', type=int, default=4)
    parser.add_argument('--db', default='/tmp/vault-scale-check.sqlite3')
    parser.add_argument('--folder', default='/tmp/vault-scale-input')
    parser.add_argument('--fresh', action='store_true')
    parser.add_argument('--no-idempotency', action='store_true',
                        help='skip the re-import check (slow on huge runs)')
    parser.add_argument('--fast', action='store_true',
                        help='pass --fast through to importfolder')
    parser.add_argument('--batch', type=int, default=20_000)
    parser.add_argument('--domains', type=int, default=400)
    parser.add_argument('--hot-domain-share', type=float, default=0.3)
    parser.add_argument('--hot-password-share', type=float, default=0.001)
    args = parser.parse_args()

    settings.DATABASES['default']['NAME'] = args.db

    from vault.importer import collect_files
    from vault.models import Account, Email, Pass, Url

    if args.fresh or not os.path.exists(args.db):
        if os.path.exists(args.db):
            os.remove(args.db)
        call_command('migrate', 'vault', verbosity=0, interactive=False)

    files = collect_files([args.folder]) if os.path.isdir(args.folder) else []
    if not files or args.fresh:
        print(f'Generating {human(args.records)} lines...')
        domains = [f'{chr(97 + i % 26)}{chr(97 + (i // 26) % 26)}'
                   f'{chr(97 + (i // 676) % 26)}' for i in range(args.domains)]
        sample_path, sample_line = generate_folder(
            args.folder, args.records, args.files, domains,
            args.hot_domain_share, args.hot_password_share,
        )
    else:
        print(f'Reusing {args.folder} ({len(files)} file(s)); '
              f're-run with --fresh to rebuild.')
        with open(os.path.join(args.folder, 'part-000.txt'),
                  encoding='utf-8') as handle:
            sample_line = handle.readline().strip()
    print(f'Sample line: {sample_line}')

    from vault.utils import parse_line
    parsed = parse_line(sample_line)
    sample_url = parsed['url']
    sample_identifier = parsed['account_identifier']

    existing = Account.objects.count()
    if existing:
        print(f'Database already holds {existing:,} records; measuring only.')
    else:
        print(f'\nImporting {args.folder} ...')
        command = ['importfolder', args.folder, '--quiet',
                   '--batch', str(args.batch)]
        if args.fast:
            command.append('--fast')
        started = time.perf_counter()
        call_command(*command)
        elapsed = time.perf_counter() - started
        total = Account.objects.count()
        print(f'  {total:,} records in {elapsed:.1f}s '
              f'({total / elapsed:,.0f} records/s)')

    total = Account.objects.count()
    print(f'\nSearch behaviour on {total:,} records '
          f'({Url.objects.count():,} URLs, {Email.objects.count():,} emails, '
          f'{Pass.objects.count():,} passwords)')

    from vault.views import filter_accounts, _reference_id_query

    def run_search(**params):
        qs = filter_accounts(params)
        return qs.count()

    print()
    domain_count, _ = timed('search: exact domain (gmail.com), count only',
                            lambda: run_search(field='domain', q='gmail.com'))
    timed('search: full URL of one line',
          lambda: run_search(field='url', q=sample_url))
    timed('search: exact email of one account',
          lambda: run_search(field='email', q=sample_identifier))
    timed('search: shared password (large result set)',
          lambda: run_search(field='password', q='ReusedPass123'))
    counts = {'domain': domain_count,
              'password': Pass.objects.filter(
                  name='ReusedPass123').first().accounts.count()}
    print(f'  (shared password matches {counts["password"]:,} records, '
          f'gmail.com matches {counts["domain"]:,})')

    def page():
        qs = (filter_accounts({'field': 'domain', 'q': 'gmail.com'})
              .order_by('-created_at', '-id')[:100])
        return len(list(qs))

    timed('search: first page of a 100k+ result set (100 rows)', page,
          expect=min(100, counts['domain']))

    print('\nQuery plans (what makes it fast):')
    from django.db.models.functions import Lower as _Lower
    ids_sql, ids_params = (
        Url.objects.annotate(match_key=_Lower('name'))
        .filter(match_key='gmail.com').values('id').query.sql_with_params()
    )
    print('  url lookup  :', query_plan(ids_sql, ids_params))
    account_sql, account_params = (
        Account.objects.filter(
            url_id__in=_reference_id_query(Url, 'host', 'gmail.com')
        ).values('id').query.sql_with_params()
    )
    print('  account scan:', query_plan(account_sql, account_params))

    print('\nExport completeness (through the real view):')
    from django.test import Client
    client = Client()
    started = time.perf_counter()
    response = client.get('/export/csv/',
                          {'field': 'domain', 'q': 'gmail.com'})
    header_seen = False
    rows = 0
    for chunk in response.streaming_content:
        rows += chunk.count(b'\n')
        header_seen = True
    export_seconds = time.perf_counter() - started
    body_rows = rows - (1 if header_seen else 0)
    verdict = ('complete' if body_rows == counts['domain'] else 'TRUNCATED')
    print(f'  CSV rows streamed: {body_rows:,} vs {counts["domain"]:,} '
          f'matching records -> {verdict} ({export_seconds:.1f}s)')

    response = client.get('/export/json/', {'field': 'domain', 'q': 'gmail.com'})
    json_bytes = sum(len(chunk) for chunk in response.streaming_content)
    print(f'  JSON streamed: {human(json_bytes)} bytes')

    started = time.perf_counter()
    response = client.get('/search/', {'field': 'domain', 'q': 'gmail.com'})
    html_seconds = (time.perf_counter() - started) * 1000
    print(f'  /search page rendered: {response.status_code} in '
          f'{html_seconds:.0f} ms for {total:,} records')

    if args.no_idempotency:
        return

    print('\nIdempotency (importing the same folder twice must add nothing):')
    before = Account.objects.count()
    command = ['importfolder', args.folder, '--quiet', '--batch',
               str(args.batch)]
    if args.fast:
        command.append('--fast')
    call_command(*command)
    after = Account.objects.count()
    print(f'  {before:,} -> {after:,} '
          f'({"ok, nothing duplicated" if before == after else "PROBLEM"})')


if __name__ == '__main__':
    main()
