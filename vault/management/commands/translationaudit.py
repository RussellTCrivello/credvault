"""
Report interface strings that the translation catalogues do not cover.

``makemessages`` needs the GNU gettext tools, which are not always installed.
This command does the part that does not need them: it reads the templates and
Python of an app, collects every msgid those sources declare, and compares the
result with each catalogue under ``locale/``.

    python manage.py translationaudit             # report
    python manage.py translationaudit --strict    # nonzero exit when incomplete

It deliberately does not modify the ``.po`` files: deciding what a string
should say in another language is a translator's job, not a script's.
"""
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

SINGLE = r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\''  # a source literal
RUN = r'(?:%s)(?:[ \t\n]*(?:%s))*' % (SINGLE, SINGLE)

TPL_TRANS = re.compile(
    r'{%+\s*(?:trans|translate)\s+(?P<q>' + SINGLE + r')'
)
TPL_BLOCK = re.compile(
    r'{%+\s*(?:blocktrans|blocktranslate)(?P<opts>[^{}]*?)' + '%}'
    r'(?P<body>.*?)' + r'{%+\s*end(?:blocktrans|blocktranslate)\s*' + '%}',
    re.S,
)
PY_GETTEXT = re.compile(
    r'(?:_lazy|gettext_lazy|ngettext_lazy|_)\s*\(\s*(?P<q>' + RUN + r')'
)
# ``ngettext(one, other, number)`` takes its singular form as the msgid.
PY_NGETTEXT = re.compile(
    r'ngettext\w*\s*\(\s*(?P<one>' + RUN + r')\s*,\s*(?P<other>' + RUN + r')'
)
# A msgid can be spread over several quoted lines, so an entry is read as a
# run of quoted strings rather than a single one.
PO_ENTRY = re.compile(r'^msgid (?P<q>(?:"(?:[^"\\\n]|\\.)*\n?)+)', re.M)
TEMPLATE_VAR = re.compile(r'\{\{\s*(\w+)\s*\}\}')


class Command(BaseCommand):
    help = (__doc__ or '').strip().splitlines()[0]

    def add_arguments(self, parser):
        parser.add_argument(
            '--app', default='vault',
            help='App whose sources are scanned (default: vault).',
        )
        parser.add_argument(
            '--locale', action='append', default=None,
            help='Only check these locales (default: every catalogue found).',
        )
        parser.add_argument(
            '--strict', action='store_true',
            help='Exit with status 1 when a catalogue is missing strings.',
        )

    def handle(self, *args, **options):
        root = Path('.')
        app_dir = root / options['app']
        if not app_dir.is_dir():
            raise CommandError('No such app directory: %s' % app_dir)

        used = self.scan(app_dir)
        locales = options['locale'] or self.locale_dirs(root / 'locale')
        if not locales:
            raise CommandError('No catalogues found under locale/.')

        incomplete = 0
        self.stdout.write('%s string(s) in use' % len(used))
        for code in locales:
            path = root / 'locale' / code / 'LC_MESSAGES' / 'django.po'
            if not path.exists():
                self.stdout.write(self.style.WARNING(
                    '%s: no catalogue at %s' % (code, path)))
                incomplete += 1
                continue
            known = self.catalog(path)
            missing = sorted(used - known)
            unused = sorted(known - used)
            if missing:
                incomplete += 1
            self.stdout.write('%s: %s entries, %s missing, %s unused'
                              % (code, len(known), len(missing), len(unused)))
            for msgid in missing:
                self.stdout.write(self.style.ERROR('  missing: %s' % msgid))
            # Entries this scan cannot see are not necessarily stale: they may
            # belong to another app or to Django's own templates.
            for msgid in unused:
                self.stdout.write(self.style.WARNING(
                    '  not in %s/**: %s' % (options['app'], msgid)))

        if options['strict'] and incomplete:
            raise CommandError(
                '%s catalogue(s) need attention.' % incomplete
            )
        if not incomplete:
            self.stdout.write(
                self.style.SUCCESS('Every catalogue covers the app.')
            )

    # -- scanning ---------------------------------------------------------
    def scan(self, app_dir):
        found = set()
        for path in sorted(app_dir.rglob('*.html')):
            found |= self.scan_template(path.read_text(encoding='utf-8'))
        for path in sorted(app_dir.rglob('*.py')):
            if path.name == 'tests.py':
                continue
            found |= self.scan_python(path.read_text(encoding='utf-8'))
        return found

    def scan_template(self, text):
        out = set()
        for match in TPL_TRANS.finditer(text):
            out.add(self.literal(match.group('q')))
        for match in TPL_BLOCK.finditer(text):
            body = match.group('body')
            one = re.sub(r'\s+', ' ', body.split('{% plural %}')[0]).strip()
            # ``{% blocktrans %}`` hands ``{{ name }}`` to gettext as
            # ``%(name)s``, which is the msgid the catalogue has to carry.
            out.add(TEMPLATE_VAR.sub(r'%(\1)s', one))
        return {value for value in out if value}

    def scan_python(self, text):
        out = {self.literal(match.group('q'))
               for match in PY_GETTEXT.finditer(text)}
        for match in PY_NGETTEXT.finditer(text):
            out.add(self.literal(match.group('one')))
        return {value for value in out if value}

    def literal(self, value):
        """Join a run of adjacent literals and normalise its whitespace."""
        parts = re.findall(r'''(["'])((?:(?!\1).|\\.)*)\1''', value, re.S)
        text = ''.join(body for _quote, body in parts)
        text = (text.replace("\\'", "'").replace('\\"', '"')
                .replace('\\n', ' ').replace('\\t', ' '))
        return re.sub(r'\s+', ' ', text).strip()

    def catalog(self, path):
        text = path.read_text(encoding='utf-8')
        found = set()
        for match in PO_ENTRY.finditer(text):
            msgid = self.literal(match.group('q'))
            if msgid:
                found.add(msgid)
        return found

    def locale_dirs(self, locale_root):
        if not locale_root.is_dir():
            return []
        return sorted(
            entry.name for entry in locale_root.iterdir()
            if (entry / 'LC_MESSAGES' / 'django.po').exists()
        )
