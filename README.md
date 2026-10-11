# LogIntel workspace

A small Django workspace for a vault of login records: five reference tables
(URL, email, username, password, IP) that each store a value once, and records
that point at those values. The interface is plain Django templates plus a
handful of progressive enhancements — every screen works with JavaScript
turned off.

```bash
python manage.py migrate
python manage.py runserver
```

## Screens

| Route | What it does |
| --- | --- |
| `/` | Overview: counts, the five reference tables, the tools used most. |
| `/records/` | The quick filter: one field, one exact value. |
| `/search/` | The same filter bar with the result table and its exports. |
| `/advanced-search/` | Every field at once, grouped, combined with AND. |
| `/import/` | Bulk ingest from uploads or a server path, with live progress. |
| `/references/<kind>/` | One reference table: find, add, delete a value. |
| `/records/<id>/`, `/records/<id>/edit/`, `/records/<id>/delete/` | A single record. |

## Filtering

The three filter screens share one system, so they behave the same way:

* **Segmented controls are radio buttons.** Choosing a field or an order is a
  real form control, not a script-built widget, so a filter can be bookmarked,
  shared and pressed with the keyboard.
* **Active filters are chips.** Each chip links back to the same URL minus its
  own parameter, so one click removes exactly one condition.
* **A field selector alone is not a filter.** Records are never dumped: the
  list appears once a value is given, which is what keeps a workspace of
  millions of rows responsive.
* **`/` focuses the value field**, Escape clears it, and the last few values
  per screen are remembered in `localStorage` as one-click chips.

The exact-value quick search is answered by an index; `Contains` scans. The
reference tables expose both as `match=prefix` (the default, index-backed) and
`match=contains`, with `sort=name|name_desc|used|recent`.

## Importing

`/import/` ingests text rows separated by a colon, pipe, tab or semicolon:

```
https://example.com/login:user@example.com|secret|203.0.113.10
```

Two sources, one engine:

* **From this device** — a file, a whole folder, or files and folders dropped
  onto the page. The browser sends the folder's relative paths, so the
  progress list shows `2019/old.log`, not a nameless `old.log`.
* **From a server path** — a directory or single file already on disk, read
  recursively. Enable it deliberately on a host where the workspace is
  reachable by people who should not read arbitrary paths:

  ```python
  VAULT_ALLOW_LOCAL_IMPORT_PATHS = False   # admin_LogIntel/settings.py
  ```

Progress is measured **in bytes, not lines**: `vault.importer` counts the bytes
it has read and pushes deltas to a tracker, so the bar reflects the work done
rather than a guess. Line counts are reported alongside — read, empty or
comment, malformed, valid, stored, already stored — and every line is
classified exactly once, so `lines == blank + invalid + valid` and
`valid == created + duplicates` hold in the final summary. Numbers are only
ever monotonic; a percentage is not shown until a size is known.

While the import POST is in flight the browser polls `vault:import_job_status`,
which reads the counters of the job running in the request thread
(`vault/import_jobs.py`, an in-process registry). **Multiple workers or servers
need a shared cache** — swap the registry for one and nothing else changes.
`Stop` sets an event that the reader checks at chunk boundaries; batches
already written stay written.

Without JavaScript the same form posts normally and answers with a summary
message. The CLI path is
`python manage.py importfolder <path> [--ext txt|--no-bar|--quiet]`, which
prints the same live bar to a terminal.

## Appearance

Light and dark themes are toggled from the sidebar and remembered; the choice
applies before first paint so the page does not flash. Icons are one inline SVG
sprite defined in `vault/templatetags/monitoring.py` — `<use>` references, so
an icon inherits text colour and needs no per-theme asset. Arabic is a full
right-to-left layout, not a mirrored one: logical CSS properties
(`padding-inline-start`, `border-inline-end`) keep both directions correct.

## LogIntel language support

The workspace uses Django's translation framework. English is the default language; Arabic is available with a right-to-left layout. The language selector applies the choice across workspace pages and remembers it for the session.

## Add another language

1. Add its Django language code and display name to `LANGUAGES` in `admin_LogIntel/settings.py`.
2. Create or update its catalog with `python manage.py makemessages -l <code>`.
3. Translate `locale/<code>/LC_MESSAGES/django.po`.
4. Compile catalogs with `python manage.py compilemessages`.
5. Add or update interface tests for the new language, especially direction and translated form labels.

New templates and Python interface strings should use Django's `{% trans %}` / `{% blocktrans %}` tags and `gettext_lazy` / `gettext`, respectively, so they remain available to translators.

### When gettext is not installed

`makemessages` and `compilemessages` need the GNU gettext tools. Without them,
`python manage.py translationaudit` still answers the question that matters —
which interface strings a catalogue does not cover:

```bash
python manage.py translationaudit            # report
python manage.py translationaudit --strict   # nonzero exit when incomplete
```

It reads `{% trans %}`, `{% blocktrans %}`, `_()` and `ngettext()` from the app
and compares them with every catalogue in `locale/`; it never edits a `.po`.
The compiled `.mo` files in this repository are written with
[`polib`](https://pypi.org/project/polib/) in that situation, which produces the
same bytes `msgfmt` would.

## Tests

```bash
python manage.py test vault
```

The suite covers the record and reference model (values reused, never
duplicated), every screen's rendered contract in both languages, the import
engine's counters and cancellation, the JSON endpoints the progress panel
polls, and that the catalogue stays complete.
