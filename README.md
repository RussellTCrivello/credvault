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

# Search semantics

There are two deliberate tiers of matching, and both are settled behavior:

| Page | Matching | Backed by |
| --- | --- | --- |
| Search (quick search) | **Exact**, case-insensitive, on the chosen field (`domain` also matches paths under the domain) | B-tree indexes on the reference tables |
| Advanced search (per-field filters and the free-text keyword) | **Substring** ("contains"), case-insensitive, inside the value | `pg_trgm` GIN indexes added by migration `0003` |

The decision, made after the PostgreSQL move, is to keep substring matching
on Advanced search rather than switch it to prefix matching or full-text
ranking:

* The Advanced search form promises it: the filters are labelled "URL
  contains", "Email contains", and so on, and keyword search is expected to
  find `corp` inside `https://vpn.corp.example.net/remote`.
* The `pg_trgm` indexes exist precisely to make `LIKE '%text%'` fast; a
  keyword substring search over 1,000,000 records measured about 0.2 s on
  PostgreSQL 16 (2 vCPU) when the database was moved over, so the concern
  that motivated the question is already answered.
* Prefix matching would silently miss values with the term in the middle --
  the common case for this workspace -- and full-text ranking would change
  which records are returned (stemming, stop words) and adds nothing for
  passwords, emails and host names, which are not prose.

Quick search stays exact on purpose: it is the fast, predictable path, and
the search page says so. The two tiers are locked in by the tests
(`test_advanced_search_matches_substrings`,
`test_quick_search_stays_exact_for_non_domain_values` in
`vault/tests.py`).

The "`domain` also matches paths under the domain" half of quick search is
implemented as B-tree range scans (`lower(name) >= 'https://example.com/'`
up to the highest code point). Those comparisons run under `COLLATE "C"`
(byte order), backed by the `vault_url_name_lower_c_idx` index added by
migration `0006`. Byte order is the only collation in which a
prefix-plus-upper-bound range means exactly "starts with the prefix": in a
locale collation -- the default on clusters initialized by the Windows
installer, for example -- the upper bound does not reliably sort above
ordinary text, and the same scan silently matches nothing. Comparing under
`"C"` makes domain search behave identically in every database collation,
and the index keeps it a fast, indexed tier alongside the exact matches.

# Import jobs and recovery

The web import page (including its streaming progress mode) and the
`importfolder` management command use the durable `ImportJob` engine. Each
source line receives a hash-only `ImportRecord` outcome; account rows, ledger
outcomes, counters, and the gap-free checkpoint commit together in one
PostgreSQL transaction per batch. The durable transaction explicitly sets
`synchronous_commit = on`, and the job-row lease is fenced at each batch so a
stale worker cannot commit or release a replacement worker's lease.

The streaming import UI receives the durable job ID as soon as its manifest is
recorded. The Import jobs list and job detail page show database-backed status,
stage, progress, and counters; Pause, Continue, and Stop requests update the
job row and take effect at a durable batch boundary. Pause keeps the live
worker at that checkpoint until continued or cancelled. Path-backed jobs can
also be resumed after a worker exits if their source manifest still matches.

The path-backed engine records a source manifest (ordered relative filenames,
byte sizes, and content digests) and validates it before and after import or
recovery. The streaming progress view combines its exact line counts with that
first manifest scan instead of reading every source once just to count lines;
the engine still revalidates the manifest before importing. While streaming,
it hashes every byte and checks file metadata at durable batch boundaries, so
a changed batch is rejected before commit without a filesystem stat on every
line. A changed source is refused rather than replayed under old line
positions. Upload bytes are not retained on disk: upload jobs keep a manifest
hash and ledger, hash the bytes as they are streamed, and compare that digest
before completion. Duplicate account fingerprints are resolved in bounded
bulk lookups rather than one database query per line, and ordinary contiguous
batches advance their checkpoint without re-reading all new ledger positions;
recovery still uses the gap scan for out-of-order positions. An interrupted
upload with unaccounted lines cannot be replayed after its live request worker
exits. While that worker is alive, Pause
holds it at a committed checkpoint and Continue resumes the same in-memory
upload stream; after a process/server restart, upload the source again to
replay unaccounted lines. The lower-level `vault.importer` bulk functions
remain for compatibility and performance tests; they do not create durable
jobs or per-line ledgers and are not called by the production web/management
entry points.

Redirect behavior after an import (settled, and asserted by
`vault/tests.py`):

* **One job** -- a file/folder upload or a single path -- redirects to the
  job detail page `/import/jobs/<job-id>/`, where the job's status, stage,
  progress bar and committed/skipped/failed counters can be inspected.
* **Several jobs** -- one per path listed in the text area -- redirects to
  the job list `/import/jobs/`.
* **The streaming progress flow** (the JavaScript uploader,
  `X-Import-Progress: 1`) keeps its own contract: the final NDJSON `done`
  event carries the redirect to `/records/` for the browser.

From the command line, `python manage.py recoverimports` lists abandoned,
replayable jobs and `python manage.py recoverimports --run [--job UUID]`
recovers them with the same code as the Resume button (a live lease is never
taken over). A recovery whose log outcome is `partial` replayed missing lines;
it is not an error. See `docs/OPERATIONS_AND_RECOVERY.md`.

Recovery: `recover_job()` reconciles the ledger against the database,
persists the recalculated counters, and -- when the job's source can be
re-read (path-based imports store `source_path` and the extension filter)
-- resumes exactly the source positions the ledger has not accounted for,
keeping each line's original position and idempotency key. Uploaded files
are not kept on disk, so their jobs are reconciled and settled, but cannot
be resumed automatically. The **Resume** button on the job page runs the
same recovery. A `pending` job with no live owner, or a `processing` job with
an expired/missing lease, counts as abandoned and may be taken over; a live
lease blocks recovery. Recovery logs distinguish
clean completion, partial recovery, and failure; an existing `failed` ledger
row is not reported as a clean recovery.

A real subprocess regression test exits with `os._exit` immediately after one
batch commits, then verifies that recovery preserves that batch and imports
the remaining path lines exactly once. This verifies that specific process
crash/acknowledgement-loss window only. It does not simulate PostgreSQL
process failure, host/power loss, storage-controller behavior, replica
failover, or every possible termination point; durability still depends on
PostgreSQL's WAL/fsync configuration and the storage stack honoring flushes.
Do not interpret the test suite as a blanket proof of production crash
safety.

# Performance

Two sets of numbers, both measured with the code in this repository:

* **Import** -- on a small development sandbox: 2 vCPU, 4 GB RAM,
  PostgreSQL 16.2 over a local socket with default server settings, one local
  disk, and 1,000,000 synthetic lines (about 95 MB) whose values are almost
  all new. That is the worst case for the reference tables, because every
  record adds a row to each of the five tables and updates every index on
  them.
* **Queries** -- the same sandbox and the same 1,000,000 records.

A fast machine (more cores, 64 GB RAM, an SSD) is faster than this; what
matters below is the size of each effect, and "Tuning for a fast machine"
turns those effects into settings.

The sandbox's disk was slow and at times throttled (one checkpoint spent 269 s
writing), so the exact times moved between runs; each comparison below was run
with the same data on the same box, which is what makes the differences worth
reading.

## Import speed

| Import | Batch | Parse workers | 1,000,000 lines | Speed |
| --- | --- | --- | --- | --- |
| Before this change | 100,000 | one process | 170 s | 5,900 / s |
| This change, same batch | 100,000 | 2 | 133 s | 7,500 / s |
| This change, default batch | 250,000 | 2 | 141-148 s | about 7,000 / s |
| This change, default batch, maximum speed | 250,000 | 2 | 130 s (116 s load + 14 s index rebuild) | 7,700 / s |

The parse workers are the largest single gain: parsing used to run between the
database batches, and now it runs while the previous batch is being copied and
merged. Parsing alone runs at about 60,000 lines/s on one core, which is why
overlapping it matters more than making it faster.

Maximum speed then removes the second largest cost, the per-row update of the
five trigram indexes, and rebuilds them once at the end. In these runs it was
worth about 12% over the whole load (148 s to 130 s) and about 22% on the load
itself (148 s to 116 s, then 14 s to build the five indexes again); the gain
grows with the size of the database, because updating an index costs more the
larger it is, while rebuilding it once does not.

### Batch size

Rows per COPY batch, one run each, same data and the same two workers:

| Rows per batch | 50,000 | 100,000 | 250,000 | 500,000 | 1,000,000 |
| --- | --- | --- | --- | --- | --- |
| Load | 153 s | 133 s | 141 s | 146 s | 171 s |

Between 100,000 and 500,000 the difference is inside the run-to-run noise of
this disk (roughly 10%); 1,000,000 and above measured about 20% slower. There
is a reason to expect that shape: the parser has to fill a batch before any of
it can be copied and merged, so a batch as large as the input removes the
overlap the parse workers exist to create, and one oversized set-based insert
has to do all of its planning and joining in one go. The default is therefore
250,000 rows -- twice the earlier 100,000 -- and it is not raised automatically
on a machine with more memory. Use `VAULT_IMPORT_BATCH_ROWS` and
`vault/tests_performance.py` to find your own machine's number.

### Durable streaming path

The table above measures the compatibility bulk loader; it does not include a
per-line durable ledger or source-integrity passes. An opt-in durable-path
measurement now includes a source manifest scan, pre-import revalidation,
stream parsing, account/reference writes, ledger entries, and checkpoints:

```bash
VAULT_RUN_PERFORMANCE=1 VAULT_PERF_LINES=20000 \
POSTGRES_PASSWORD='choose-a-password' \
.venv/bin/python manage.py test \
  vault.tests_performance.ImportThroughputTests.test_durable_path_import_throughput
```

On the isolated sandbox PostgreSQL test cluster, 20,000 synthetic unique
records took 4.5 seconds (4,431 records/s), and 100,000 took 26.5 seconds
(3,774 records/s), both with 50,000-line durable batches. These are engine-path
measurements, not HTTP benchmarks or a one-million-line run; they are
machine-specific results and are not directly comparable to the bulk-loader
table above. The durable benchmark can be scaled with `VAULT_PERF_LINES` on
the PostgreSQL instance you intend to use.

## Tuning for a fast machine

The settings below are written for a fast single machine -- for example 3
CPUs, 64 GB RAM and an SSD, with a 6 GB graphics card in the slot. They are
what actually changes the import and search speed of this workspace.

### What the importer uses

| Resource | Used by | Setting on that machine |
| --- | --- | --- |
| CPU | parsing (`VAULT_IMPORT_WORKERS` processes) and PostgreSQL (COPY, the set-based inserts, the index rebuilds) | `VAULT_IMPORT_WORKERS=2` on 3 cores, leaving one core for PostgreSQL; try `3` and compare with `vault/tests_performance.py` |
| RAM | PostgreSQL's page cache and `work_mem` for the batch joins; the importer's own buffer (about 90 MB at the default batch size) | `shared_buffers=16GB`, `effective_cache_size=48GB`, `work_mem=256MB`, `maintenance_work_mem=2GB` |
| Disk | the WAL, the reference tables and the five trigram indexes | keep the cluster on the SSD; `random_page_cost=1.1`, `max_wal_size=8GB`, `checkpoint_timeout=30min`, `wal_compression=on` |
| Graphics card | nothing | 6 GB are not used by any part of this workspace |

**The graphics card does not help.** PostgreSQL, Django and this importer are
CPU- and disk-bound: the fingerprints are computed with `sha256` in the
PostgreSQL backend, the trigram index is built by `pg_trgm` in the same
backend, and the parsing is plain Python. None of those has a GPU path, so the
6 GB card changes neither the import nor the searches. Keep it for other work
(for example a local model), and spend the tuning time on `postgresql.conf`
and the importer's three knobs instead.

### A command for millions of lines

```bash
export VAULT_IMPORT_WORKERS=2               # 3 CPUs: leave one for PostgreSQL
export VAULT_IMPORT_BATCH_ROWS=250000       # the default; try 500000 as well
export VAULT_MAINTENANCE_WORK_MEM=2GB       # used to rebuild the search indexes
POSTGRES_PASSWORD='choose-a-password' .venv/bin/python manage.py importfolder \
    /data/folder-one /data/folder-two /data/archive --ext '*' --fast
```

* `--fast` drops the five trigram search indexes for the duration and rebuilds
  them once at the end -- including when the import fails. Exact searches keep
  working throughout; substring (advanced) searches are slower while the
  import runs. `VAULT_FAST_MODE_SCOPE=all` restores the older, much broader
  behaviour (every non-unique btree index as well); use it only for an offline
  load. **Measured:** into an existing 880k-account database, fast mode was
  *slower* end to end than the default online mode and made searches 5-10x
  slower; see "Fast mode versus online ingestion" below.
* Any number of paths in one command, each read recursively, all in one job,
  with per-file counts on the terminal.
* `--ext '*'` reads every file in the folders; the default is `.txt`.
* The same options exist on the **Import records** page: the maximum-speed
  checkbox, one folder or file path per line in the text area, and a file
  picker that accepts any number of files in one selection.
* Parse workers start with `fork` where POSIX provides it and with `spawn`
  where it does not. On Windows an explicit `VAULT_IMPORT_WORKERS` greater
  than 1 is honored through a spawn pool (each worker pays a one-off Python
  startup; parsing results are identical -- locked in by
  `test_spawn_workers_store_the_same_records_as_inline_parsing`); without
  the setting, Windows parses inline in one process. Frozen Windows
  executables must call `multiprocessing.freeze_support()` at startup
  before workers can be used.

### `postgresql.conf`

```ini
shared_buffers = 16GB          # about a quarter of 64 GB
effective_cache_size = 48GB    # what the planner may assume is cached
work_mem = 256MB               # per join/sort; the batch SQL uses a few at once
maintenance_work_mem = 2GB     # index builds (the importer raises it per batch too)
max_wal_size = 8GB             # fewer checkpoints during a long load
checkpoint_timeout = 30min
wal_compression = on
random_page_cost = 1.1         # SSD
max_parallel_maintenance_workers = 3
```

Durability contract: the durable engine (web import page and `importfolder`)
sets `SET LOCAL synchronous_commit = on` for every batch transaction, so a
batch is acknowledged only after its WAL is flushed; this is asserted by
`vault/tests_import_reliability.py`. Only the lower-level compatibility loader
in `vault.importer` (used by tests and `bulk_import_lines`, not by any
production entry point) uses `synchronous_commit = off`; a power loss can lose
its last batches but not corrupt data. The importer also sets
`maintenance_work_mem` for the index rebuild; `VAULT_MAINTENANCE_WORK_MEM`
overrides the 1 GB default. Measured cost of `on` versus `off` in the
sandbox: about 0.1 ms per commit (see `benchmarks/results/2026-10-18/summary.txt`);
that is not representative of network storage.

Optional, for a load of many millions of rows, stop autovacuum from competing
for CPU and I/O while the tables are being filled, then let the planner catch
up:

```sql
ALTER TABLE vault_account SET (autovacuum_enabled = false);
-- the same for vault_url, vault_email, vault_user, vault_pass, vault_ip
-- ... run the import ...
ALTER TABLE vault_account SET (autovacuum_enabled = true);
VACUUM (ANALYZE);
```

The importer already runs `ANALYZE` on every staging batch, so the final
`VACUUM (ANALYZE)` is a one-off cleanup rather than a requirement.

### Measure your own machine

```bash
VAULT_RUN_PERFORMANCE=1 VAULT_PERF_LINES=1000000 VAULT_PERF_FAST=1 \
POSTGRES_PASSWORD='choose-a-password' \
.venv/bin/python manage.py test vault.tests_performance
```

It prints lines per second for whatever you exported. Run it a second time
without `VAULT_PERF_FAST=1`, and once with `VAULT_IMPORT_WORKERS=3`, to see
which of the three knobs pays on your machine. Watch a running import with
`select * from pg_stat_activity` (or `pg_stat_progress_copy`); whichever side
sits at 100% CPU tells you whether the workers or PostgreSQL are the limit.

## Fast mode versus online ingestion

Measured on 2 vCPU / 3.9 GB, a 200,000-line durable import into a database that
already held 879,519 accounts, with two query workers running the real search
views in a loop (`benchmarks/`, full tables in `docs/BENCHMARK_RESULTS.md`):

| Mode | Import time | Accounts/s | exact-email p95 during import | broad substring URL p50 during import |
| --- | --- | --- | --- | --- |
| Online (default), indexes maintained | 51-53 s | 3,342-3,431 | 42-56 ms | 262-268 ms |
| `--fast`, five trigram indexes suspended | 102.7 s | 1,714 | 42 ms | 1,325 ms |
| `--fast` with every btree index also suspended (old behaviour) | 77.2 s | 2,280 | 782 ms (7 samples) | 728 ms |

(The last row ran before migration 0008; its online baseline on that schema was
68.6-69.9 s, 2,516-2,564 accounts/s. Single runs on a shared VM; two runs of the
online mode differed by 2-3 %.)

In this regime the default online path is the fastest *and* keeps search
available, so `--fast` is no longer recommended for incremental loads. It may
still pay off for a bulk load into a nearly empty database (the measurements
above in "Import speed"), which this change did not re-measure.

## Counts and pagination contract

* **Default (unchanged):** exact `COUNT(*)` for the matching set and
  page-number/offset pagination. Counts are exact and consistent with the
  snapshot of that request; page numbers can shift while rows are inserted.
* **Cursor mode (opt-in):** `?sort=id&after=<id>` (ascending) or
  `?sort=-id&after=<id>` (descending, `after=0` starts at the newest row) on
  the Records, Search and Advanced search pages. No total is computed and the
  page says so ("Cursor mode: total not computed"). A row is neither repeated
  nor skipped when rows are inserted during the walk (rows behind the cursor
  are reached later; rows ahead of a descending cursor are not shown), and the
  cost of any page equals the cost of the first. Measured on 880k accounts:
  the page at 50 % depth costs 0.07-0.3 ms / 3 buffers by keyset versus 44-46 ms
  / 1,205 buffers by `OFFSET`. Other sort orders keep offset pagination.
* The count is never estimated under an "exact" label. Exact counts grow
  linearly with the table (50 ms for 880k accounts); see
  `docs/DATABASE_OPTIMIZATION_REPORT.md` for when to switch the dashboard
  count to a cached value (needs a product decision on its freshness).

## Query speed

One request through the Django test client, including template rendering,
against the same 1,000,000 records on the same small sandbox:

| Page | Time |
| --- | --- |
| Overview (dashboard) | 12 ms |
| Records page (first page) | 7 ms |
| Search, exact email | 11 ms |
| Search, exact password | 10 ms |
| Search, exact IP | 15 ms |
| Search, domain | 25 ms |
| Reference table page (with usage) | 29 ms |
| Advanced search, substring keyword | 97 ms |
| Advanced search, substring URL filter | 155 ms |

Exact lookups stay in the tens of milliseconds because every reference value
is reached through its unique index. Substring searches are fast for the same
reason the Advanced-search page can keep promising "contains": the `pg_trgm`
GIN index answers them (`Bitmap Index Scan on vault_url_name_trgm`), instead
of reading the whole table as the search page did before the PostgreSQL move.

## Storage

About 1.5 GB for 1,000,000 records with three million distinct reference
values (URLs, emails, passwords) on this sandbox: `vault_account` 553 MB,
`vault_url` 426 MB, `vault_email` 310 MB, `vault_pass` 225 MB, `vault_ip`
13 MB -- roughly 1.5 KB per record including every index. The trigram indexes
are the cheaper half of that: on `vault_url`, 44 MB against 264 MB of the
B-tree indexes beside it, which is why dropping them around a large load is
worth doing even though they have to be built again.

The earlier revision of this section carried SQLite numbers (about 3.6 GB for
4,000,000 records, an import at 10,500 records/s into an empty database and
3,800 records/s into a busy one, and a substring search of 14 s over
4,000,000 records, because SQLite has no trigram index). They are kept here as
history; the PostgreSQL numbers above replaced them.

## What changed

* Import: lines are parsed in worker processes, staged in a temporary table
  with COPY, and merged with set-based `INSERT ... ON CONFLICT DO NOTHING ...
  SELECT`. Batches default to 250,000 rows (`VAULT_IMPORT_BATCH_ROWS`), parse
  workers to `min(4, CPU count)` (`VAULT_IMPORT_WORKERS`).
* Import, maximum speed: `--fast` on `importfolder`, or the checkbox on the
  import page, suspends the five trigram search indexes for the duration and
  rebuilds them once at the end (`VAULT_MAINTENANCE_WORK_MEM` sizes the
  rebuild).
* Import inputs: one or many uploaded files, a whole folder from the browser,
  or any number of folder/file paths (one per line) on the page; any number of
  paths on the `importfolder` command.
* Records loaded in bulk and records saved through the ORM produce the same
  fingerprint (`vault/tests_performance.py` checks it).
* Counts and pagination: list, search, overview, import-job, and import-ledger
  pages use exact queryset totals and have no 10,000-row ceiling; page links can
  reach every matching row, and displayed counts do not use a `+` estimate.
  Exact PostgreSQL counts may take time for broad queries on very large tables;
  result rows remain paginated and exports stream every matching record.
* Reference pages: "usage" is counted only for the values on the current page,
  not for the whole reference table.
* Search: exact (B-tree) on the search page, substring through the `pg_trgm`
  GIN indexes on Advanced search -- see "Search semantics".

## Limits

One PostgreSQL server on one machine is a good fit for tens of millions of
records. At the measured sizes, a billion records would need days of importing
(speed falls as the data grows) and roughly 1.5 TB of storage. Reaching
billions takes partitioning or a distributed store, parallel COPY into it,
enough RAM to keep the indexes hot, and the same fast-mode trick for the
indexes that cannot stay hot.
