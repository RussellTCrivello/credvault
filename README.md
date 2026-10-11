# LogIntel vault

A Django workspace for a vault of login records: every record is the
relationship between a URL, an account identifier (an email **or** a
username), a password, and an IP address.

The project is built around two things:

* **Nothing is entered twice.** The five shared values live in their own
  reference tables and every record points at them through foreign keys.
* **It stays usable at volume.** Millions of lines can be ingested from a
  folder, an exact value search comes back in milliseconds, and an export
  covers the whole answer rather than the page on screen.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver 0.0.0.0:8000
```

`sample_data/` holds a few hundred importable lines (plus files and lines that
are deliberately *not* importable) so the folder flow can be tried immediately.

## Data model

| Table | Fields |
| --- | --- |
| `Url` | Name of the URL, construction date, date of adjustment (plus a derived, indexed `host`) |
| `Email` | Name of the email, construction date, date of adjustment |
| `User` | Name of the user, construction date, date of adjustment |
| `Pass` | Name of the pass, construction date, date of adjustment |
| `Ip` | Name of the IP, construction date, date of adjustment |
| `Account` | url + account type + email/user + password + ip + the two dates |

`Account` is what the interface calls a **record**. The identical
relationship can never be stored twice: a `UNIQUE` index over the
foreign-key combination (NULL-aware, see
`vault/migrations/0002_account_relationship_unique.py`) is enforced by the
database, so a re-run of any import is idempotent without the importer ever
having to read the existing table.

`Url.host` is the one column that is not user-editable. It is derived on write
so that a *domain* search is an equality lookup on an index instead of a
`LIKE '%domain%'` scan of every URL.

## Ingesting a folder

Both paths read **every line of every file**, keep the lines that meet the
requirements, and ignore the rest. Files are streamed in batches, so the limit
is disk, not memory.

*Web* — `/import/` accepts one or more files, a whole folder picked in the
browser (`webkitdirectory`, subfolders included), or an absolute path to a
folder that already lives on the server. Non-text files are skipped;
`IMPORT_EXTENSIONS` in settings controls which extensions count as text and
`IMPORT_ROOTS` restricts the server-path field (empty = any readable path,
which is the right default for a local tool).

*Server side* — recommended for the largest volumes:

```bash
.venv/bin/python manage.py importfolder /path/to/folder            # recursive
.venv/bin/python manage.py importfolder /a /b --ext csv --ext txt  # narrow the types
.venv/bin/python manage.py importfolder /path --all-extensions
.venv/bin/python manage.py importfolder /path --dry-run --show-errors 10
.venv/bin/python manage.py importfolder /path --fast               # WAL + synchronous=NORMAL
```

The accepted line format is unchanged from the original CLI: `url`,
`identifier`, `password`, and an optional `ip`, separated by a colon, pipe,
tab, or semicolon. Empty lines, comments, and decorative borders are ignored,
and a line whose values do not fit the columns is skipped rather than
truncated. `--dry-run` reports what *would* be stored and shows the first
rejected lines, which is the fastest way to answer "why did this file import
nothing?".

`vault/importer.py` does the work: a batch's distinct values are collected, its
missing reference rows are inserted with conflict-ignore semantics, and the
relationship rows follow in one bulk statement. No per-line query, no
per-line object.

## Searching, at scale

`/search/` is deliberately *not* a fuzzy box. You pick the field — domain, full
URL, email, username, password, IP — and type the exact value. The lookup hits
an index on the reference table, then the relationship index on `Account`, which
also answers the newest-first ordering, so the cost follows the size of the
*result*, not of the table. Case is ignored; partial matches are not (that is
what `/advanced-search/` is for, with per-field `contains` filters and
sorting).

The result header reports the total and how long the query took. Pagination is
a presentation detail: **exports always cover every matching record**, streamed
straight from the database so a million-row export does not need a million-row
response body in memory.

Measured with `scripts/scale_check.py` on a laptop-class sandbox (2 CPUs,
SQLite), against a workspace of **2,000,000 records** (160k URLs, 1.2M emails,
864k passwords):

| Operation | Result |
| --- | --- |
| import a folder of 2,000,000 lines (8 files) | 267 s (~7.5k lines/s, ~14k row inserts/s) |
| exact email / username / password / IP lookup | 1–2 ms |
| exact full-URL lookup | ~18 ms |
| exact domain lookup that matches **600,143** records (count) | ~39 ms |
| first page of those 600,143 results | ~9 ms |
| `/search/` page end to end (view + template) | ~80 ms |
| CSV export of all 600,143 matches | 17 s, 600,143 rows — complete |
| re-importing the same folder | 0 new records, values reused |

```bash
.venv/bin/python scripts/scale_check.py --records 200000              # quick
.venv/bin/python scripts/scale_check.py --records 2000000 --fast      # serious
```

It writes its own database under `/tmp`, never your workspace file.

## Exports

`/export/csv/` and `/export/json/` stream `ID, Domain, URL, Account type,
Identifier, Password, IP address, Construction date, Date of adjustment`.
Any search or advanced-search filter in the query string is honoured, which is
how a result page exports its full answer. The CSV starts with a UTF-8 BOM so a
spreadsheet opens Arabic values correctly. Treat exports as sensitive: they
include the passwords.

## Django admin

The five reference tables and `Account` are all registered, each capped at 100
rows per page, with exact-match search on the indexed value columns.

## Language support

The workspace uses Django's translation framework. English is the default
language; Arabic is available with a right-to-left layout. The language
selector applies the choice across workspace pages and remembers it for the
session.

Because the sandbox/CI image has no GNU gettext, use the bundled helper instead
of `compilemessages`:

```bash
.venv/bin/python scripts/compile_catalogs.py           # report + rebuild every .mo
.venv/bin/python scripts/compile_catalogs.py --check    # fail on untranslated msgids
```

### Add another language

1. Add its Django language code and display name to `LANGUAGES` in
   `admin_LogIntel/settings.py`.
2. Create or update its catalog with `.venv/bin/python manage.py makemessages -l <code>`.
3. Translate `locale/<code>/LC_MESSAGES/django.po`.
4. Compile catalogs with `.venv/bin/python scripts/compile_catalogs.py`.
5. Add or update interface tests for the new language, especially direction and
   translated form labels.

New templates and Python interface strings should use Django's `{% trans %}` /
`{% blocktrans %}` tags and `gettext_lazy` / `gettext`, respectively, so they
remain available to translators. Anything defined at module import time (a
form label table, a CSV header) must use `gettext_lazy`, or it is frozen in the
language that happened to be active when the process started.

## Tests

```bash
.venv/bin/python manage.py test vault
```

37 tests cover the pages, the reference-reuse rules, the duplicate guard, bulk
folder import (browser upload, server folder, management command, dry run),
exactness of each search field, pagination, exports that must include every
match, delete protection, and the Arabic RTL interface.
