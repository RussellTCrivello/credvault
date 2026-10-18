"""
Parse workers safe for Windows spawn.

These functions run inside multiprocessing Pool workers. On POSIX the pool
uses fork (cheap), on Windows it uses spawn which re-imports this module in a
fresh interpreter without Django apps loaded. Therefore this module must NOT
import Django models at top level — only stdlib and vault.utils / validators
which are pure Python.

Both functions are deliberately self-contained and do not touch the database.
"""

import hashlib

from vault.utils import OversizedSourceLine


def _hash_line(line):
    if isinstance(line, OversizedSourceLine):
        return line.source_hash
    if isinstance(line, str):
        line = line.encode('utf-8', errors='replace')
    elif not isinstance(line, (bytes, bytearray)):
        line = str(line).encode('utf-8', errors='replace')
    return hashlib.sha256(line).hexdigest()


def _idem_key(job_id_s, pos):
    return hashlib.sha256(f"{job_id_s}:{pos}".encode('utf-8')).hexdigest()[:64]


def _parse_durable_chunk(payload):
    """Parse a chunk of (position, line) pairs in a worker process.

    Runs without touching the database. Returns a list of
    (row_or_None, meta_or_outcome) in the same order as the input chunk.
    """
    chunk, job_id_str = payload
    # Local imports to keep the worker self-contained (spawn context).
    from vault.utils import parse_line as _parse_line
    from vault.utils import NO_IP as _NO_IP

    def _row_from_parsed_local(record_data):
        ip_value = record_data['ip_address']
        if not ip_value or ip_value == _NO_IP:
            ip_value = None
        return (
            record_data['url'],
            record_data['account_type'],
            record_data['account_identifier'],
            record_data['secret_data'],
            ip_value,
        )

    out = []
    for pos, line in chunk:
        h = _hash_line(line)
        k = _idem_key(job_id_str, pos)
        meta = {
            'source_position': pos,
            'source_hash': h,
            'idempotency_key': k,
        }
        try:
            rec = _parse_line(line)
        except Exception as exc:
            out.append((None, {
                **meta,
                'status': 'failed',
                'error_category': 'parse_error',
                'error_message': f'Line parser raised {type(exc).__name__}.',
                'retryable': True,
            }))
        else:
            if rec is None:
                out.append((None, {
                    **meta,
                    'status': 'skipped',
                    'error_category': (
                        'line_too_large'
                        if isinstance(line, OversizedSourceLine)
                        else 'invalid_format'
                    ),
                    'retryable': False,
                }))
            else:
                try:
                    row = _row_from_parsed_local(rec)
                except Exception:
                    out.append((None, {
                        **meta,
                        'status': 'skipped',
                        'error_category': 'invalid_format',
                        'retryable': False,
                    }))
                else:
                    out.append((row, meta))
    return out


def _parse_chunk(lines):
    """
    Parse and canonicalize a chunk of raw lines.

    Runs inside a worker process when the session uses parse workers, and in
    the main process otherwise. Returns (rows, skipped).

    Self-contained: does NOT import vault.importer (which imports Django),
    only vault.utils and vault.validators which are pure Python.
    """
    from vault.utils import parse_line as _parse_line
    from vault.utils import NO_IP as _NO_IP
    from vault.utils import CanonicalRecord as _CanonicalRecord
    from vault.validators import (
        classify_identifier as _classify_identifier,
        normalize_ip as _normalize_ip,
        normalize_password as _normalize_password,
        normalize_url as _normalize_url,
    )

    def _canonical_record(result):
        if not result:
            return None
        if isinstance(result, _CanonicalRecord):
            return result
        url = _normalize_url(result.get('url'))
        password = _normalize_password(result.get('secret_data'))
        account_type, identifier = _classify_identifier(
            result.get('account_identifier')
        )
        ip_value = _normalize_ip(result.get('ip_address'))
        if not (url and password and identifier and account_type):
            return None
        return {
            'url': url,
            'account_type': account_type,
            'account_identifier': identifier,
            'secret_data': password,
            'ip_address': ip_value,
        }

    def _row_from(record):
        ip_value = record['ip_address']
        if not ip_value or ip_value == _NO_IP:
            ip_value = None
        return (
            record['url'],
            record['account_type'],
            record['account_identifier'],
            record['secret_data'],
            ip_value,
        )

    rows = []
    skipped = 0
    for line in lines:
        record = _canonical_record(_parse_line(line))
        if record is None:
            skipped += 1
            continue
        rows.append(_row_from(record))
    return rows, skipped
