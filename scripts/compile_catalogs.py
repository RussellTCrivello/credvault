#!/usr/bin/env python3
"""
Compile the ``.po`` catalogs in ``locale/`` into the ``.mo`` files Django
reads, and report what is still untranslated.

Django's ``manage.py compilemessages`` shells out to GNU ``msgfmt``, which is
not installed everywhere (CI containers and sandboxes usually lack it). This
does the same job with pure Python so the Arabic interface can never drift out
of sync with the strings in the code:

    python scripts/compile_catalogs.py            # check + compile
    python scripts/compile_catalogs.py --check    # fail on untranslated msgids

Add the translatable strings to the matching ``locale/<code>/LC_MESSAGES/django.po``
first (or run ``manage.py makemessages`` where gettext is available).
"""

import argparse
import os

import polib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCALE = os.path.join(REPO, 'locale')


def languages():
    for code in sorted(os.listdir(LOCALE)):
        path = os.path.join(LOCALE, code, 'LC_MESSAGES', 'django.po')
        if os.path.isfile(path):
            yield code, path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true',
                        help='exit non-zero if anything is untranslated')
    parser.add_argument('--no-write', action='store_true',
                        help='report only, do not rewrite the .mo files')
    args = parser.parse_args()

    status = 0
    for code, po_path in languages():
        po = polib.pofile(po_path)
        untranslated = po.untranslated_entries()
        complete = len(po) - len(untranslated)
        print(f'{code}: {complete}/{len(po)} translated '
              f'({po.percent_translated() if len(po) else 100}%)')
        for entry in untranslated[:40]:
            status = 1
            print(f'  untranslated: {entry.msgid!r}'
                  + (f' / {entry.msgid_plural!r}' if entry.msgid_plural else ''))

        if args.no_write:
            continue
        mo_path = os.path.join(os.path.dirname(po_path), 'django.mo')
        # Django ignores an entry whose msgstr is empty, so leaving the
        # untranslated ones out of the .mo keeps the English source text.
        po.save_as_mofile(mo_path)
        print(f'  wrote {os.path.relpath(mo_path, REPO)}')

    if args.check and status:
        print('untranslated strings found')
    return status


if __name__ == '__main__':
    raise SystemExit(main())
