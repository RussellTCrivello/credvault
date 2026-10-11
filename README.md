# Data formats

The workspace stores five kinds of value -- URL, email, username, password and
IP address. Every value that enters it, whether typed on the Add/Edit Record
form, added on a reference page, or read from an imported file, is checked
against the format of its kind and stored in one canonical spelling. That is
what makes the two rules of this project hold:

* **Accuracy** -- a value that does not match its format is refused (the form
  shows why, an imported line is counted as skipped) instead of being stored
  half-parsed.
* **No duplicates** -- because a value is always stored in the same spelling,
  the unique index on each reference table collapses every spelling of the
  same value onto one row, and the record fingerprint does the same for whole
  records.

| Kind | Format | Stored as |
| --- | --- | --- |
| URL | `[http:// \| https://] host.extension [:port] [/path] [?query] [#fragment]` -- the scheme is optional and defaults to `https://`; at most 2,000 UTF-8 bytes (the database index limit) | scheme and host lower-cased, default port (`80`/`443`) dropped, `%xx` escapes upper-cased, no trailing `/` on a bare origin, IDN hosts in punycode |
| Email | `name@domain.extension` -- letters, digits and `. _ + - ! # $ % & ' * / = ? ^ \` { } ~` before the `@`, dots between atoms only | fully lower-cased |
| Username | letters (any script), digits and `. _ - +`, single spaces inside, starting and ending with a letter or digit, no `@` | exactly as typed (case matters) |
| Password | any printable text, 1-500 characters | exactly as typed, edges trimmed |
| IP address | four numbers of 0-255 separated by dots (`192.168.1.1`), or an IPv6 address | canonical compressed form; `N/A`, `-`, `null`, `none`, `nil` and `unknown` mean "no address" |

Rejected examples: `not a url`, `example` (no extension), `example.c`,
`ftp://example.com`, `a@@b.com`, `user@`, `a..b@example.com`, `a@b.c`,
`.bob`, `bob@x.com` (as a username), `192.168.1`, `192.168.001.001`,
`999.1.1.1`, `192.168.1.1/24`.

## Imported files

One record per line, in this order:

```
URL <sep> email-or-username <sep> password [<sep> IP address]
```

`<sep>` is `|`, a tab, `;` or `:`. The identifier is an email when it holds a
valid address, otherwise a username. The IP address is optional; because a
password may contain the separator, the last field is treated as the address
only when it really is one. Blank lines, `#` comments and decorative box
characters are ignored.

```
https://example.com/login|person@example.com|Secret 1|203.0.113.10
https://example.com/login;bob;Secret 2;N/A
https://example.com/login:bob:Secret:3:1.2.3.4
```
python manage.py createsuperuser

The rules live in `vault/validators.py`, which is the single place to change
if a format needs adjusting.

# Speed, PostgreSQL and the Settings page

The vault runs on SQLite by default, with no setup. For large volumes use
PostgreSQL, which imports and searches much faster.

## Running on PostgreSQL

1. Install PostgreSQL 14 or newer with the `pg_trgm` extension, which is part
   of the standard contrib modules (on Debian and Ubuntu, the
   `postgresql-contrib` package). Check it with
   `SELECT * FROM pg_available_extensions WHERE name = 'pg_trgm';`.
2. Create an empty database and a role that can create tables and extensions.
3. Set the environment and migrate:

```sh
pip install -r requirements.txt
export CREDVAULT_DB_ENGINE=postgresql
export CREDVAULT_PG_NAME=credvault CREDVAULT_PG_USER=credvault
export CREDVAULT_PG_PASSWORD=... CREDVAULT_PG_HOST=localhost
python manage.py migrate
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `CREDVAULT_DB_ENGINE` | `sqlite` | `sqlite` or `postgresql` |
| `CREDVAULT_SQLITE_PATH` | `db.sqlite3` in the project folder | SQLite database file |
| `CREDVAULT_PG_NAME`, `_USER`, `_PASSWORD`, `_HOST`, `_PORT` | `credvault`, `postgres`, empty, `localhost`, `5432` | PostgreSQL connection |
| `CREDVAULT_PG_CONN_MAX_AGE` | `600` | Seconds a connection is kept open |
| `CREDVAULT_TRIGRAM_SEARCH` | `on` | `off` skips the `pg_trgm` extension and its contains-search indexes |

Migrations `0003` and `0004` add the search indexes. On PostgreSQL, `0004`
builds them with `CREATE INDEX CONCURRENTLY`, so the tables stay readable while
the indexes are built.

## Speed modes (Settings page)

The Settings page chooses one import mode and one search mode. The modes change
how the work is done, never which records exist or which records match.

| Import mode | What it does |
| --- | --- |
| Balanced (default) | Counts the lines first for an exact progress bar, one process, and every commit fully durable. On PostgreSQL the records are streamed with `COPY`; on SQLite they are written through the ORM. |
| Fast | Skips the line count and commits asynchronously in larger batches. On PostgreSQL each batch is streamed with `COPY` into a staging table and deduplicated in the database. Progress is measured in bytes, so the file is not read twice. On SQLite it uses the ORM writer with larger batches. |
| Maximum speed | Fast, plus lines are parsed in parallel worker processes and batches are larger. |

| Search mode | Exact match counts up to | Results per page |
| --- | --- | --- |
| Balanced (default) | 100,000 | 100 |
| Fast | 10,000 | 100 |
| Maximum speed | 1,000 | 50 |

A count above the limit is shown as `N+`. Paging and export still return every
matching record. The same modes can be chosen on the command line:
`python manage.py importfolder <path> --mode fast`.

Searches on PostgreSQL use trigram (`pg_trgm`) indexes for contains search,
prefix indexes for "starts with", and literal id lists so that PostgreSQL can
combine the per-column indexes. Record lists are read through indexes ordered
newest-first, so a page costs the same whatever the total size. Totals for the
whole table come from the planner's estimate and are shown with `≈`.

## Benchmarking on your hardware

Run the benchmark on an empty scratch database, because it refuses to run on a
database that already holds records (unless `--force` is given):

```sh
python manage.py benchmark --rows 1000000 --import-mode fast --query-mode balanced
python manage.py benchmark --file /path/to/records.txt --import-mode maximum
```

It imports the data, then times each search through the web views, and prints
the import rate in records per second. Numbers depend heavily on the disk, the
CPU cores and the PostgreSQL settings (`shared_buffers`, `max_wal_size`,
`synchronous_commit`), so measure on the machine you will use.

## Measured results

Measured with `manage.py benchmark` on a 2-vCPU sandbox with about 4 GB of RAM,
PostgreSQL 16.14 (`shared_buffers` 768 MB, `synchronous_commit` off on the
server), and the same 1,000,000-line file (71 MB) for every run. Each run started
from an empty database. Your numbers will differ.

| Import, 1,000,000 records | Before this change | Balanced | Fast | Maximum speed |
| --- | --- | --- | --- | --- |
| SQLite | 238.5 s (4,193/s) | 233.5 s (4,282/s) | not measured | not measured |
| PostgreSQL | 314.1 s (3,183/s) | 145.7 s (6,861/s); repeat 139.1 s (7,189/s) | 135.4 s (7,385/s) | 126.7 s (7,892/s) |

| Search, 1,000,000 records (PostgreSQL, Balanced search mode) | Before this change | Now |
| --- | --- | --- |
| Dashboard totals | 291.9 ms | 21.2 ms |
| Exact email | not recorded | 16.2 ms |
| Exact password | not recorded | 12.0 ms |
| Domain | not recorded | 26.0 ms |
| Any field | 1,091.1 ms | 13.0 ms |
| Advanced contains text | 1,937.6 ms | 631.4 ms |
| Reference URL list (100 rows, with usage counts) | 1,165.8 ms | 117.7 ms |

The search column is from the repeat Balanced run. The first Balanced run after a
fresh import measured 3,219.6 ms for the advanced contains search; three later
searches on the same database measured 510 to 575 ms, and the repeat run 631 ms.
The cause of that one slow reading is unknown, so treat the advanced contains
figure as a range of about 0.5 to 0.7 seconds. The reference URL list ran in
about 105 to 118 ms in every run.

On this machine the database writes are the bottleneck. Parsing alone takes about
14 seconds per million lines, and Maximum speed adds little over Fast (126.7 s
against 135.4 s) because it only adds a second parsing process. Expect a larger gap on a
server with more cores and faster storage, but do not assume it: the target of
100,000 records per second has not been reached in these tests.

## Scale

Import cost grows with the number of records. On the test machine described
above, a million records take about 2 to 2.5 minutes, so one billion records would
take roughly 35 to 40 hours. The test data is synthetic: the database held
1.3 GB after importing one million records, including indexes, so one billion
records would need on the order of 1.3 TB of disk for this data. Real records
will differ in size. The 100,000-records-per-second target is not met by this
design on this hardware, and the billion-record figures are extrapolations, not
measurements.

Searches use index lookups, so exact and prefix searches should stay fast as the
data grows. Only sizes up to one million records were measured. A very broad search (for example a contains search that matches most of the
table) still reads many index entries.

# LogIntel language support

The workspace uses Django's translation framework. English is the default language; Arabic is available with a right-to-left layout. The language selector applies the choice across workspace pages and remembers it for the session.

## Add another language

1. Add its Django language code and display name to `LANGUAGES` in `admin_LogIntel/settings.py`.
2. Create or update its catalog with `python manage.py makemessages -l <code>`.
3. Translate `locale/<code>/LC_MESSAGES/django.po`.
4. Compile catalogs with `python manage.py compilemessages`.
5. Add or update interface tests for the new language, especially direction and translated form labels.

New templates and Python interface strings should use Django's `{% trans %}` / `{% blocktrans %}` tags and `gettext_lazy` / `gettext`, respectively, so they remain available to translators.
