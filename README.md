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
| URL | `[http:// \| https://] host.extension [:port] [/path] [?query] [#fragment]` -- the scheme is optional and defaults to `https://` | scheme and host lower-cased, default port (`80`/`443`) dropped, `%xx` escapes upper-cased, no trailing `/` on a bare origin, IDN hosts in punycode |
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

# LogIntel language support

The workspace uses Django's translation framework. English is the default language; Arabic is available with a right-to-left layout. The language selector applies the choice across workspace pages and remembers it for the session.

## Add another language

1. Add its Django language code and display name to `LANGUAGES` in `admin_LogIntel/settings.py`.
2. Create or update its catalog with `python manage.py makemessages -l <code>`.
3. Translate `locale/<code>/LC_MESSAGES/django.po`.
4. Compile catalogs with `python manage.py compilemessages`.
5. Add or update interface tests for the new language, especially direction and translated form labels.

New templates and Python interface strings should use Django's `{% trans %}` / `{% blocktrans %}` tags and `gettext_lazy` / `gettext`, respectively, so they remain available to translators.

# Database: PostgreSQL

The workspace stores its data in PostgreSQL (version 14 or newer). The
PostgreSQL contrib modules must be installed; they are included in the
standard packages, and the migration enables `pg_trgm` for substring search.

## Set up a database

Run these as a PostgreSQL superuser (for example `sudo -u postgres psql`),
choosing your own password:

```sql
CREATE ROLE credvault LOGIN PASSWORD 'choose-a-password' CREATEDB;
CREATE DATABASE credvault OWNER credvault;
```

`CREATEDB` lets the test suite create its temporary test database.

Connection settings come from environment variables (defaults in brackets):

| Variable | Meaning |
| --- | --- |
| `POSTGRES_DB` | database name (`credvault`) |
| `POSTGRES_USER` | role that owns the database (`credvault`) |
| `POSTGRES_PASSWORD` | role password (empty) |
| `POSTGRES_HOST` | server host or socket directory (`127.0.0.1`) |
| `POSTGRES_PORT` | server port (`5432`) |

Then install the Python dependencies and create the schema:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
export POSTGRES_PASSWORD='choose-a-password'
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver 0.0.0.0:8000
```

## Move existing SQLite data

If you have an older `db.sqlite3`, copy it into an empty PostgreSQL database
(run `migrate` first, as above):

```bash
.venv/bin/python manage.py copy_from_sqlite /path/to/db.sqlite3
```

The command copies every table in one transaction, including the
administrator accounts, groups, sessions and admin history, and keeps the
original ids. It stops if the target already holds records, and it checks
that the row counts match and that every stored fingerprint is correct. If any
check fails, nothing is saved.

## Tests

```bash
POSTGRES_PASSWORD='choose-a-password' .venv/bin/python manage.py test vault
```

# Performance

All numbers below were measured with the code in this repository on a
development box (2 vCPU, 4 GB RAM, SQLite 3.40, WAL mode) using synthetic data
(about 2 million URLs, 4 million passwords, 3.5 million IP addresses). Your
hardware and data will differ; run the same steps on your own machine to get
your own numbers.

## Import speed

| Run | Records | Time | Speed |
| --- | --- | --- | --- |
| Earlier ORM importer, empty database | 1,000,000 | 8 min 30 s | about 2,000 / s |
| SQL loader, empty database | 1,000,000 | 1 min 35 s | about 10,500 / s |
| SQL loader, database already holding 1,000,000 | 3,000,000 | 13 min 7 s | about 3,800 / s |

Parsing alone runs at about 60,000 lines per second on one core, so the
remaining time is database work. Speed falls as the database grows past the
page cache, because each new record touches several random index pages.

## Query speed

Each time is one request through the Django test client, including template
rendering.

| Page | 1M records | 4M records |
| --- | --- | --- |
| Overview (dashboard) | 41 ms | 82 ms |
| Search, exact email | 16 ms | 36 ms |
| Search, exact password | 8 ms | 8 ms |
| Search, exact IP | 7 ms | 7 ms |
| Search, domain | 36 ms | 301 ms |
| Reference table page (with usage) | 29 ms | 30 ms |
| Advanced search, substring keyword | 1.2 s | 14.2 s |

Exact lookups stay in milliseconds because every reference value is found
through its unique index. Substring searches (`LIKE '%text%'`) cannot use a
B-tree index, so they read the whole table and grow with it.

## Storage

About 3.6 GB for 4 million records (roughly 900 bytes per record including
the reference tables and indexes).

## What changed

* Import: records are staged in a temporary table, then reference values and
  records are inserted with set-based `INSERT OR IGNORE ... SELECT`. The
  fingerprint is computed the same way as `Account.save()`, so records loaded
  in bulk and records saved one by one are identical (see
  `vault/tests_performance.py`).
* Counts: list, search and overview totals stop at `VAULT_COUNT_CAP` (default
  10,000) and show "10,000+", so a page never runs a full `COUNT(*)`. Exports
  still stream every matching record.
* Reference pages: "usage" is counted only for the values on the current page,
  not for the whole reference table.
* SQLite is configured in `admin_LogIntel/settings.py` (WAL journal, a 256 MB
  page cache, in-memory temporary tables).

## Limits

SQLite on one machine is a good fit for tens of millions of records. At the
measured rates, a billion records would take days to import (import speed
falls as the data grows) and about 900 GB of storage, and substring search
would take hours. Reaching billions with few-second imports and searches needs
a server database with bulk-load tools (such as PostgreSQL `COPY`), enough RAM
to keep the indexes in memory, and a text index for substring search.
