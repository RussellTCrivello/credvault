"""Explain *why* lines of an import source are rejected, without reading
anything into the database and without printing any secret.

``importfolder`` counts every line the parser refuses as ``skipped``
(``invalid_format``) but does not say what was wrong with it.  This command
samples the same files with the same parser and groups the rejected lines by
the first check they fail.  Examples are shown only as a *shape mask*: every
letter becomes ``a`` and every digit ``9``, runs are collapsed, punctuation is
kept -- so a password or address can never appear in the output.

Read-only.  It never touches the database.
"""

import os
from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from vault.importer import collect_files_from_path
from vault.utils import (
    DELIMITERS,
    MAX_SOURCE_LINE_BYTES,
    _BOX_CHARS_RE,
    _decode,
    _split,
    _split_secret_and_ip,
    parse_line,
)
from vault.validators import classify_identifier, normalize_url


def mask_shape(text, limit=90):
    """Return the structure of `text` with every letter/digit masked."""
    out = []
    last = ''
    for ch in text:
        if ch.isdigit():
            token = '9'
        elif ch.isalpha():
            token = 'a'
        elif ch.isspace():
            token = '_'
        else:
            token = ch
        if token in ('a', '9', '_') and token == last:
            continue
        out.append(token)
        last = token
        if len(out) >= limit:
            out.append('...')
            break
    return ''.join(out)


def _delimiter_reason(cleaned, delimiter):
    """(rank, reason) for the first check `cleaned` fails with `delimiter`."""
    parts = _split(cleaned, delimiter)
    if parts is None:
        return 0, 'empty_field_between_separators'
    if len(parts) < 3:
        return len(parts), f'only_{len(parts)}_field(s)'
    if normalize_url(parts[0]) is None:
        return len(parts), 'first_field_not_a_valid_url'
    if classify_identifier(parts[1])[1] is None:
        return len(parts), 'second_field_not_email_or_username'
    password, _ip = _split_secret_and_ip(parts[2:], delimiter)
    if password is None:
        return len(parts), 'password_or_ip_field_invalid'
    return len(parts), 'unclassified'


def classify_rejection(raw):
    """Return a short reason why `raw` (bytes) is not a record."""
    if len(raw) > MAX_SOURCE_LINE_BYTES:
        return 'line_too_large'
    cleaned = _decode(raw).strip().lstrip('\ufeff')
    if not cleaned:
        return 'blank_line'
    if cleaned.startswith('#'):
        return 'comment'
    if _BOX_CHARS_RE.search(cleaned):
        return 'decorative_box_characters'
    candidates = [d for d in DELIMITERS if d in cleaned]
    if not candidates:
        return 'no_separator (| tab ; :)'
    best = max(
        ((_delimiter_reason(cleaned, d), d) for d in candidates),
        key=lambda item: item[0][0],
    )
    (_rank, reason), delimiter = best
    name = {'\t': 'tab'}.get(delimiter, delimiter)
    return f'{reason} [separator {name}]'


class Command(BaseCommand):
    help = (
        "Sample import files and report, per reason, why lines are rejected "
        "(read-only; examples are masked so no secret is printed)."
    )

    def add_arguments(self, parser):
        parser.add_argument('paths', nargs='+', help='Files or folders.')
        parser.add_argument('--ext', default='txt')
        parser.add_argument(
            '--limit', type=int, default=200000,
            help='Maximum lines to read per file (default 200000).',
        )
        parser.add_argument(
            '--every', type=int, default=1,
            help='Examine every Nth line (default 1) to sample more of a '
                 'big file within --limit.',
        )
        parser.add_argument('--examples', type=int, default=3)

    def handle(self, *args, **options):
        every = max(1, options['every'])
        limit = max(1, options['limit'])
        files = []
        for path in options['paths']:
            if not os.path.exists(path):
                raise CommandError(f'Path does not exist: {path}')
            files.extend(collect_files_from_path(path, ext=options['ext']))
        if not files:
            raise CommandError('No matching files found.')

        reasons = Counter()
        examples = {}
        total = accepted = 0
        for path in files:
            f_total = f_ok = 0
            with open(path, 'rb') as handle:
                index = 0
                while f_total < limit:
                    raw = handle.readline(MAX_SOURCE_LINE_BYTES + 1)
                    if not raw:
                        break
                    if len(raw) > MAX_SOURCE_LINE_BYTES and not raw.endswith(b'\n'):
                        # Drain the rest of an oversized line.
                        while True:
                            more = handle.readline(MAX_SOURCE_LINE_BYTES)
                            if not more or more.endswith(b'\n'):
                                break
                    index += 1
                    if (index - 1) % every:
                        continue
                    f_total += 1
                    if parse_line(raw) is not None:
                        f_ok += 1
                        continue
                    reason = classify_rejection(raw)
                    reasons[reason] += 1
                    bucket = examples.setdefault(reason, [])
                    if len(bucket) < options['examples']:
                        text = _decode(raw).strip()
                        bucket.append(mask_shape(text))
            total += f_total
            accepted += f_ok
            pct = (100.0 * f_ok / f_total) if f_total else 0.0
            self.stdout.write(
                f'{os.path.basename(path)}: examined {f_total:,}, '
                f'accepted {f_ok:,} ({pct:.1f}%)'
            )

        rejected = total - accepted
        self.stdout.write('')
        self.stdout.write(
            f'Examined {total:,} lines in {len(files)} file(s): '
            f'{accepted:,} accepted, {rejected:,} rejected '
            f'({(100.0 * rejected / total) if total else 0:.1f}%).'
        )
        for reason, count in reasons.most_common():
            share = 100.0 * count / rejected if rejected else 0.0
            self.stdout.write(f'\n{count:>10,}  {share:5.1f}%  {reason}')
            for shape in examples.get(reason, []):
                self.stdout.write(f'              shape: {shape}')
