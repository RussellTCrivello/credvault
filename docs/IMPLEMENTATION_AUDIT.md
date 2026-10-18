# Final implementation audit (deliverable I)

Branch `arena/b98bd230-credvault`, based on `main` @ `095abd0`. Environment:
Python 3.11.2, Django 5.2.18, psycopg 3.3.6, PostgreSQL 16.2 (disposable local
cluster; `pg_trgm` compiled from the PostgreSQL 16.2 source for the sandbox only,
not a project dependency).

## 1. Test status

| Run | Result |
| --- | --- |
| `manage.py test vault --parallel=1` (final) | **227 tests, OK, 5 skipped** (67.6 s) |
| Skipped | the five opt-in performance tests (`VAULT_RUN_PERFORMANCE`, `VAULT_RUN_CONCURRENT_PERF`, `VAULT_RUN_FILE_CONCURRENCY_PERF`) |
| Opt-in performance tests, reduced size (20k-30k lines) | 5 run, 5 OK |
| `manage.py makemigrations --check --dry-run` | no drift |
| `git diff --check` | clean |
| Baseline at the start of this program | 203 discovered, 200 passed, 3 skipped (before the new tests) |

New automated tests: `vault/tests_pagination.py` (6: ascending and descending
walks, stability under concurrent inserts, default mode unchanged, invalid
cursors fall back, no `COUNT` in cursor mode); `vault/tests_scale_changes.py`
(12: redundant indexes gone and unique ones kept, uniqueness still enforced,
job-scoped ledger lookups use an index, fast-mode scope and always-restore,
host cache equals uncached for 17 cases, cache bound, import correct with a
2-value cache ceiling, cache cleared on a failed batch); one test in
`tests_import_reliability.py` (`recoverimports`); plus the bounded-reader,
oversized-line, work-memory, byte-bounded queue, worker-count, parallel-file and
WAL/durability tests added earlier in the program in `tests.py`,
`tests_import_reliability.py` and `tests_performance.py`.

Not covered by any automated test: PostgreSQL restart, disk-full, backup
restore, failover, power loss, cold-cache performance, multi-core scaling,
anything above 1.8 M accounts.

## 2. Changed components

| File | Change |
| --- | --- |
| `vault/utils.py` | 16 KiB source-line cap, `OversizedSourceLine` (hash-only), `iter_bounded_source_lines`, size rejection in `parse_line` |
| `vault/importer.py` | byte-estimated task/batch accounting; 8 MiB / 64 MiB / 256 MiB limits; bounded file reads; `work_mem` default 64 MB, cap 256 MiB; fast-mode scope (`VAULT_FAST_MODE_SCOPE`); removed four dropped indexes from `_FAST_MODE_INDEXES`; docstring fix |
| `vault/import_engine.py` | durable batching on valid rows, outcome rows and buffered bytes; bounded parser queue; oversized-line outcome; bounded reference cache cleared on rollback; `synchronous_commit = on` documented and asserted |
| `vault/workers.py` | `line_too_large` outcome for oversized lines |
| `vault/validators.py` | 4,096-entry LRU on `normalize_host` (str input only) |
| `vault/views.py` | bounded progress queue (64 events); opt-in cursor pagination (`KeysetPage`, `_keyset_request`), `_page_total`, `after` removed from the page links |
| `vault/templates/vault/{search,browse,advanced_search,pagination}.html` | "Cursor mode: total not computed" header and a cursor Next link |
| `vault/management/commands/importfolder.py` | `--parallel-files` (1-8), bounded submission queue, connection cleanup, banner |
| `vault/management/commands/recoverimports.py` | new: list/recover abandoned jobs |
| `vault/management/commands/perfbench.py` | new: `generate`, `stages`, `queries`, `storage`, `concurrent` |
| `vault/models.py`, `vault/migrations/0008_ledger_index_cleanup.py` | drop four redundant indexes; ledger FK `db_index=False` |
| `vault/tests*.py` | tests listed above |
| `README.md` | durability contract, fast mode vs online, counts and pagination contract, `recoverimports` |
| `SCALABILITY_ASSESSMENT.md`, `docs/*.md`, `benchmarks/**` | deliverables A-I, scripts and raw results |

No settings, dependencies or `requirements.txt` entries were changed. No existing
public URL, form field or default search/pagination behaviour changed; the
cursor mode is a new opt-in query parameter. Tracked `__pycache__` files that
the test runs modified were restored.

## 3. Migration

`0008_ledger_index_cleanup` only drops indexes (see
`docs/DATABASE_OPTIMIZATION_REPORT.md` section 7). Forward 0.8 s, reverse 5.4 s on
a 1 M-row ledger; unique constraints unchanged; reversible; take a backup before
running on a very large database.

## 4. Configuration added

`VAULT_FAST_MODE_SCOPE` (`search` default, `all`), `importfolder --parallel-files`,
`manage.py recoverimports`, `manage.py perfbench`. Existing variables
(`VAULT_IMPORT_WORKERS`, `VAULT_IMPORT_BATCH_ROWS`, `VAULT_IMPORT_WORK_MEM`,
`VAULT_MAINTENANCE_WORK_MEM`) are documented in `docs/OPERATIONS_AND_RECOVERY.md`.

## 5. Acceptance criteria versus evidence

| Criterion | Status | Evidence / gap |
| --- | --- | --- |
| Streaming/segmented reading, safe boundaries, bounded memory | **met for the supported format** | sequential bounded streaming; segmentation evaluated and rejected with measurements; 637 MB peak at 1 M lines |
| Independent files processed concurrently within limits | **met, opt-in** | `--parallel-files`; mixed benefit; uniqueness verified |
| Intra-file parallelism where format permits | **not implemented (evidence-based)** | read/split 2-3 % of pre-DB work |
| Retry/resume without corruption | **met (process kill)** | SIGKILL experiment and unit tests |
| Fewer round trips; empty and populated throughput | met | sections 5 and 11 of the results |
| Dedup and idempotency under concurrency | met at small scale | 0 duplicate fingerprints in all runs |
| Durable mode per its contract | **partly verified** | contract documented and asserted; PostgreSQL restart and power loss untested |
| No sacrifice of query availability for ingest speed | met | default is online mode; `--fast` narrowed and documented as not recommended |
| Indexes justified by measurement | partly | audit table; five `*_like` reference indexes remain candidates |
| Exact and substring benchmarked separately | met | results sections 4 and 10 |
| Pagination/count contract defined | met | README + tests |
| Query latency measured during sustained ingestion | met at 0.88 M accounts, two workers, 70 s imports | not at larger scales or with exports |
| Storage growth and index overhead quantified | met | storage tables |
| Progressive-scale tests show actual capacity | **not met** | only stage 0 (0.88 M); plan documented |
| Partitioning/sharding preserves uniqueness | **not built** | analysis only |
| Billion-record support | **not claimed**; unverified projections labelled |
| Automated tests pass; docs; security | met | 227 OK |

## 6. Known limitations and unresolved items

1. Scale: nothing above 1.8 M accounts was run; all numbers are from one
   2-vCPU sandbox with a fast virtual disk. Production hardware may change
   rankings (file parallelism, `wal_compression`, batch size, fsync cost).
2. Many measurements are single runs (spread 1-3 % where repeated twice).
   Latency percentiles with fewer than 50 samples (fast-mode variants) are
   indicative only.
3. The earlier `--fast` run recorded one `OperationalError` in a query thread; its
   message was not captured and it did not recur in the later fast run. Cause unknown.
4. An earlier attempt to run the whole opt-in performance class in one process
   timed out after about 30 minutes and left an orphaned backend (killed). Each
   test passes individually; the cause of the stall was not identified.
5. `recover_job` reports outcome `partial` whenever lines were replayed, even if
   the job then completes; this is existing, tested behaviour and is now documented.
6. Uploaded (non-path) imports still cannot be replayed after the worker dies.
7. Legacy `ImportRecord.source_line` / `error_message` may hold plaintext for
   rows created before hash-only ledgers; not migrated.
8. The `perfbench concurrent` mix (7 queries, 50 ms think time, 2 workers) is
   synthetic; exports, concurrent jobs and autovacuum were not exercised.
9. Counts remain exact and linear; no cached-count implementation without
   an approved freshness contract.
10. Ledger retention, partitioning and sharding were not implemented; both need
    approvals listed in `docs/PERFORMANCE_TARGETS.md`.
11. Queue-depth, worker-utilization and DB-latency metrics are not exported.
12. The README's older "Import speed" tables describe pre-existing measurements
    on different hardware and were not re-run.

## 7. Repository state

Changes are on branch `arena/b98bd230-credvault` and are committed there (see
`git log`). No other branch was touched. Large generated data (`/tmp/bench`) is
outside the repository.
