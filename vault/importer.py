"""
High-throughput bulk importer for the normalized vault.

The previous single-file import created one row at a time with get_or_create,
which is far too slow for millions of lines. This module batches the work:

  1. Stream the input in bounded batches so memory stays flat even across
     millions of lines and thousands of files.
  2. Validate every field against its format and keep only its canonical
     spelling (see vault.validators), then collect the *distinct* referenced
     values (URLs, emails, usernames, passwords, IPs) seen in each batch.
  3. Bulk-create the missing reference rows (chunked to stay under the
     SQLite variable limit).
  4. Build the Account rows in memory, keyed by the record fingerprint, drop
     the ones already stored with a single indexed lookup per chunk, and
     bulk-create the rest.

Canonicalizing in step 2 is what keeps the reference tables free of
duplicates: the same email written as ``Person@Example.COM`` or
``person@example.com`` becomes one row, and a value that does not match its
format (``not-an-ip`` in the IP column, ``b@@ad`` in the identifier column)
is counted as skipped instead of being stored.

It is used by the web file/folder import flows and by the importfolder
management command.
"""

import os
from itertools import islice

from django.db import transaction

from .models import Account, Email, Ip, Pass, Url, User, record_fingerprint
from .utils import NO_IP, CanonicalRecord, parse_line
from .validators import (
    classify_identifier, normalize_ip, normalize_password, normalize_url,
)

# Chunk IN(...) queries to stay well below SQLite's 999-variable limit.
_CHUNK = 500
_BATCH = 2000
STREAM_BATCH_SIZE = 50000

_BINARY_EXTS = {
    ".sqlite3", ".db", ".pyc", ".pyo", ".mo", ".png", ".jpg", ".jpeg",
    ".gif", ".webp", ".ico", ".pdf", ".zip", ".tar", ".gz", ".7z", ".exe",
    ".dll", ".so", ".dylib", ".woff", ".woff2", ".ttf", ".eot",
}


def _chunks(iterable, size):
    it = iter(iterable)
    while True:
        batch = list(islice(it, size))
        if not batch:
            return
        yield batch


def _bulk_get_or_create_names(model, names):
    """
    Return a {name: id} map for every canonical name, creating missing rows.

    The names arriving here are already canonical, so the lookup is an exact
    match on the unique `name` column: a value that is already stored is
    reused, never inserted a second time.
    """
    name_set = set(names)
    mapping = {}
    if not name_set:
        return mapping

    for chunk in _chunks(name_set, _CHUNK):
        for name, pk in model.objects.filter(
            name__in=chunk
        ).values_list('name', 'id'):
            mapping[name] = pk

    missing_names = [name for name in name_set if name not in mapping]
    if missing_names:
        missing_objs = [model(name=name) for name in missing_names]
        model.objects.bulk_create(
            missing_objs, batch_size=_BATCH, ignore_conflicts=True
        )
        # Re-read only the newly created names to pick up their IDs.
        for chunk in _chunks(missing_names, _CHUNK):
            for name, pk in model.objects.filter(
                name__in=chunk
            ).values_list('name', 'id'):
                mapping[name] = pk

    return mapping


@transaction.atomic
def _flush_batch(keys, urls, emails, users, passes, ips):
    """Persist one batch of deduplicated account keys and return created count."""
    if not keys:
        return 0

    url_map = _bulk_get_or_create_names(Url, urls)
    email_map = _bulk_get_or_create_names(Email, emails)
    user_map = _bulk_get_or_create_names(User, users)
    pass_map = _bulk_get_or_create_names(Pass, passes)
    ip_map = _bulk_get_or_create_names(Ip, ips)

    # One Account per fingerprint: the same URL + identifier + password + IP
    # combination appears once in the batch no matter how many lines carried
    # it, and the unique fingerprint column keeps that true across batches,
    # files and imports.
    candidates = {}
    for (url, account_type, ident, secret, ip_value) in keys:
        url_id = url_map.get(url)
        pass_id = pass_map.get(secret)
        if not (url_id and pass_id):
            continue

        if account_type == 'email':
            email_id = email_map.get(ident)
            user_id = None
            if not email_id:
                continue
        else:
            email_id = None
            user_id = user_map.get(ident)
            if not user_id:
                continue

        ip_id = ip_map.get(ip_value) if ip_value else None
        fingerprint = record_fingerprint(
            url_id, account_type, email_id, user_id, pass_id, ip_id
        )
        if fingerprint in candidates:
            continue
        candidates[fingerprint] = Account(
            url_id=url_id,
            account_type=account_type,
            email_id=email_id,
            user_id=user_id,
            password_id=pass_id,
            ip_id=ip_id,
            fingerprint=fingerprint,
        )

    # Drop the records already stored -- one indexed lookup per chunk instead
    # of scanning every account that shares a URL or a password with the batch.
    already_stored = set()
    for chunk in _chunks(list(candidates), _CHUNK):
        already_stored.update(
            Account.objects.filter(
                fingerprint__in=chunk
            ).values_list('fingerprint', flat=True)
        )

    objs = [
        account for fingerprint, account in candidates.items()
        if fingerprint not in already_stored
    ]
    if objs:
        Account.objects.bulk_create(
            objs, batch_size=_BATCH, ignore_conflicts=True
        )

    return len(objs)


def canonical_record(result):
    """
    Validate one parsed-line dict and return it with canonical values.

    Returns None when any of the five values does not match its format, so
    that a bad line is counted as skipped instead of stored half-parsed.
    Records that arrive from `parse_line` are already validated and are
    passed straight through.
    """
    if not result:
        return None
    if isinstance(result, CanonicalRecord):
        return result

    url = normalize_url(result.get('url'))
    password = normalize_password(result.get('secret_data'))
    account_type, identifier = classify_identifier(
        result.get('account_identifier')
    )
    ip_value = normalize_ip(result.get('ip_address'))

    if not (url and password and identifier and account_type):
        return None

    return {
        'url': url,
        'account_type': account_type,
        'account_identifier': identifier,
        'secret_data': password,
        'ip_address': ip_value,
    }


def bulk_import_lines(parsed_iter):
    """
    Consume an iterable of parsed-line dicts (None for invalid lines) and
    store them efficiently in bounded streaming batches.

    Returns (accounts_created, lines_skipped).
    """
    urls = set()
    emails = set()
    users = set()
    passes = set()
    ips = set()
    keys = set()

    total_created = 0
    skipped = 0

    for result in parsed_iter:
        record = canonical_record(result)
        if record is None:
            skipped += 1
            continue

        url = record['url']
        secret = record['secret_data']
        ident = record['account_identifier']
        account_type = record['account_type']
        # parse_line reports a missing address as 'N/A'; store no relation.
        ip_value = record['ip_address']
        if not ip_value or ip_value == NO_IP:
            ip_value = None

        urls.add(url)
        passes.add(secret)
        if account_type == 'email':
            emails.add(ident)
        else:
            users.add(ident)
        if ip_value:
            ips.add(ip_value)

        keys.add((url, account_type, ident, secret, ip_value))

        if len(keys) >= STREAM_BATCH_SIZE:
            total_created += _flush_batch(
                keys, urls, emails, users, passes, ips
            )
            urls.clear()
            emails.clear()
            users.clear()
            passes.clear()
            ips.clear()
            keys.clear()

    if keys:
        total_created += _flush_batch(
            keys, urls, emails, users, passes, ips
        )

    return total_created, skipped


def _is_candidate_file(filename, ext="*"):
    lower = filename.lower()
    _, file_ext = os.path.splitext(lower)
    if file_ext in _BINARY_EXTS:
        return False
    if ext == "*" or not ext:
        return True
    normalized_ext = ext.lower().lstrip(".")
    return lower.endswith(f".{normalized_ext}")


def collect_files_from_path(target_path, ext="*"):
    """
    Return a sorted list of readable text files from a file or directory path.
    """
    if os.path.isfile(target_path):
        return [target_path]
    if not os.path.isdir(target_path):
        return []

    matched = []
    for root, dirs, filenames in os.walk(target_path):
        # Skip hidden or system directories
        dirs[:] = sorted(
            d for d in dirs
            if not d.startswith(".") and d != "__pycache__"
        )
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            if _is_candidate_file(filename, ext=ext):
                matched.append(os.path.join(root, filename))
    return matched


def import_path(target_path, ext="*", on_file=None):
    """
    Import all valid lines from a single file or recursively from every
    matching file inside a folder.

    Returns (total_created, total_skipped, files_count).
    """
    files = collect_files_from_path(target_path, ext=ext)
    if not files:
        return 0, 0, 0

    total_created = 0
    total_skipped = 0
    processed_files = 0

    for filepath in files:
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as handle:
                parsed = (parse_line(line) for line in handle)
                created, skipped = bulk_import_lines(parsed)
        except OSError:
            continue

        processed_files += 1
        total_created += created
        total_skipped += skipped
        if on_file is not None:
            on_file(filepath, created, skipped, total_created)

    return total_created, total_skipped, processed_files
