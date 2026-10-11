"""
High-throughput bulk importer for the normalized vault.

The previous single-file import created one row at a time with get_or_create,
which is far too slow for millions of lines. This module batches the work:

  1. Stream the input in bounded batches so memory stays flat even across
     millions of lines and thousands of files.
  2. Collect the *distinct* referenced values (URLs, emails, usernames,
     passwords, IPs) seen in each batch.
  3. Bulk-create the missing reference rows (chunked to stay under the
     SQLite variable limit).
  4. Build the Account rows in memory, deduplicate against only the
     matching URL/password subset in the database, and bulk-create them.

It is used by the web file/folder import flows and by the importfolder
management command.
"""

import os
from itertools import islice

from django.db import transaction

from .models import Account, Email, Ip, Pass, Url, User
from .utils import parse_line

# Chunk IN(...) queries to stay well below SQLite's 999-variable limit.
_CHUNK = 500
_BATCH = 2000
_STREAM_BATCH = 50000

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
    """Return a {name: id} map for every name, creating missing rows."""
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

    url_ids = list(url_map.values())
    pass_ids = list(pass_map.values())

    # Query existing accounts only for the URLs (or passwords) in this batch,
    # never scanning the entire Account table.
    existing = set()
    if len(url_ids) <= len(pass_ids):
        for chunk in _chunks(url_ids, _CHUNK):
            existing.update(
                Account.objects.filter(url_id__in=chunk).values_list(
                    'url_id', 'account_type', 'email_id', 'user_id',
                    'password_id', 'ip_id'
                )
            )
    else:
        for chunk in _chunks(pass_ids, _CHUNK):
            existing.update(
                Account.objects.filter(password_id__in=chunk).values_list(
                    'url_id', 'account_type', 'email_id', 'user_id',
                    'password_id', 'ip_id'
                )
            )

    objs = []
    for (url, account_type, ident, secret, ip_key) in keys:
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

        ip_id = ip_map.get(ip_key) if ip_key else None
        row_key = (url_id, account_type, email_id, user_id, pass_id, ip_id)
        if row_key in existing:
            continue

        existing.add(row_key)
        objs.append(Account(
            url_id=url_id,
            account_type=account_type,
            email_id=email_id,
            user_id=user_id,
            password_id=pass_id,
            ip_id=ip_id,
        ))

    if objs:
        Account.objects.bulk_create(
            objs, batch_size=_BATCH, ignore_conflicts=True
        )

    return len(objs)


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
        if not result:
            skipped += 1
            continue

        url = (result.get('url') or '').strip()
        secret = result.get('secret_data') or ''
        ident = (result.get('account_identifier') or '').strip()
        account_type = result.get('account_type') or 'user'
        ip_value = (result.get('ip_address') or '').strip()

        if not (url and secret and ident):
            skipped += 1
            continue

        if len(url) > 2048 or len(ident) > 255 or len(secret) > 500:
            skipped += 1
            continue

        urls.add(url)
        passes.add(secret)
        if account_type == 'email':
            emails.add(ident)
        else:
            users.add(ident)

        ip_key = None
        if ip_value and ip_value.upper() != 'N/A' and len(ip_value) <= 45:
            ips.add(ip_value)
            ip_key = ip_value

        keys.add((url, account_type, ident, secret, ip_key))

        if len(keys) >= _STREAM_BATCH:
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
