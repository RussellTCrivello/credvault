"""
Synthetic data and timings for measuring the workspace on your own hardware.

``python manage.py benchmark`` writes realistic record lines to a temporary
file, imports them with the chosen import mode, and then times the searches a
user actually runs. Use it on an empty scratch database: the generated records
stay in the database afterwards.
"""

import os
import random
import tempfile
import time
from contextlib import contextmanager

from django.db import connection
from django.test import Client

DOMAIN_SUFFIXES = ('com', 'net', 'org', 'io', 'co.uk', 'de')
PATHS = (
    '/login', '/signin', '/account/login', '/auth', '/user/session',
    '/wp-login.php',
)


def write_synthetic_file(path, rows, seed=7):
    """Write ``rows`` realistic record lines to ``path`` (deterministic per seed)."""
    rnd = random.Random(seed)
    domain_count = max(1000, rows // 50)
    domains = [
        f'site{number}.{rnd.choice(DOMAIN_SUFFIXES)}'
        for number in range(domain_count)
    ]
    password_pool = max(1000, int(rows * 0.7))
    with open(path, 'w', encoding='utf-8') as handle:
        buffer = []
        for index in range(rows):
            host = domains[rnd.randrange(domain_count)]
            url = f'https://{host}{rnd.choice(PATHS)}'
            if rnd.random() < 0.5:
                identifier = f'user{index}@{host}'
            else:
                identifier = f'user{index}_{rnd.randrange(999)}'
            secret = f'pw{rnd.randrange(password_pool)}!Z'
            if rnd.random() < 0.15:
                address = 'N/A'
            else:
                address = (
                    f'{rnd.randrange(1, 223)}.{rnd.randrange(256)}.'
                    f'{rnd.randrange(256)}.{rnd.randrange(1, 255)}'
                )
            separator = '|' if index % 3 else ';'
            buffer.append(
                f'{url}{separator}{identifier}{separator}{secret}'
                f'{separator}{address}\n'
            )
            if len(buffer) >= 100_000:
                handle.writelines(buffer)
                buffer.clear()
        handle.writelines(buffer)
    return path


@contextmanager
def temporary_directory():
    directory = tempfile.mkdtemp(prefix='credvault-benchmark-')
    try:
        yield directory
    finally:
        for name in os.listdir(directory):
            os.remove(os.path.join(directory, name))
        os.rmdir(directory)


def timed_get(client, route, params=None):
    """Return ``(status_code, milliseconds)`` for one GET request."""
    started = time.perf_counter()
    response = client.get(route, params or {})
    elapsed = (time.perf_counter() - started) * 1000
    return response.status_code, elapsed


def sample_values():
    """A real email, password and domain from the database, for timed searches."""
    from .models import Account, Pass

    values = {}
    account = Account.objects.select_related('email', 'password', 'url').filter(
        account_type='email',
    ).first()
    if account is not None:
        values['email'] = account.email.name
        values['domain'] = account.url.name.split('/')[2]
    password = Pass.objects.order_by('-id').values_list('name', flat=True).first()
    if password is not None:
        values['password'] = password
    return values


def run_queries(client, values, query_label):
    """Time the searches a user runs. Yields ``(label, status, ms)``."""
    email = values.get('email', 'user1@example.com')
    password = values.get('password', 'pw1!Z')
    domain = values.get('domain', 'example.com')
    cases = (
        ('Dashboard (record total)', '/', {}),
        ('Search: exact email', '/search/', {'field': 'email', 'q': email}),
        ('Search: exact password', '/search/', {'field': 'password', 'q': password}),
        ('Search: domain', '/search/', {'field': 'domain', 'q': domain}),
        ('Search: any field', '/search/', {'q': email}),
        ('Advanced: contains text', '/advanced-search/', {'keyword': 'site12'}),
        ('Reference tables: URL page', '/references/url/', {}),
    )
    for label, route, params in cases:
        status, elapsed = timed_get(client, route, params)
        yield f'{query_label}{label}', status, elapsed


def benchmark_client(admin_user):
    client = Client()
    client.force_login(admin_user)
    return client


def database_vendor():
    return connection.vendor
