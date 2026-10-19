# Final implementation audit (deliverable I)

Branch `arena/b98bd230-credvault`, based on `main` @ `095abd0`. Environment:
Python 3.11.2, Django 5.2.18, psycopg 3.3.6, PostgreSQL 16.2 (disposable local
cluster; `pg_trgm` compiled from the PostgreSQL 16.2 source for the sandbox only,
not a project dependency).

## 1. Test status

| Run | Result |
| --- | --- |
| `manage.py test vault --parallel=1` (final) | **259 tests, OK, 5 skipped** |
| Skipped | the five opt-in performance tests (`VAULT_RUN_PERFORMANCE`, `VAULT_RUN_CONCURRENT_PERF`, `VAULT_RUN_FILE_CONCURRENCY_PERF`) |
| Opt-in performance tests (`VAULT_PERF_LINES=50000`) | all pass after the two fixes of section 6.3: 4 of 4 verification runs of the concurrent + file-concurrency pair (before: 2 of 2 failed with `deadlock detected`, and one hung for 25 minutes) |
| `manage.py makemigrations --check --dry-run` | no drift |
| Baseline at the start of this program | 203 discovered, 200 passed, 3 skipped (before the new tests) |

New automated tests (all in `vault/`): `tests_pagination.py` (6),
`tests_scale_changes.py` (12 + 2 insert-order + 2 stale-statistics),
`tests_counts.py` (19: exact below the cap, `N+` above, no estimate labelled
exact, cached totals, TTL, invalidation on UI add/delete, cap=0 default),
`tests_import_metrics.py` (7: counters, ratios, no source values in logs, interval
0 disables), one `recoverimports` test, plus the bounded-reader, oversized-line,
work-memory, byte-bounded queue, worker-count, parallel-file and WAL/durability
tests added earlier.

Verified by drills rather than unit tests (scripts in `benchmarks/`, results in
`docs/BENCHMARK_RESULTS.md` sections 9, 17, 18): importer SIGKILL, PostgreSQL
`-m immediate` stop mid-import (twice), physical backup + WAL archive + PITR.

Not covered by any test or drill: disk-full, failover/replicas, power loss on a
disk that loses acknowledged writes, cold-cache performance, multi-core scaling,
anything above 8.27 M accounts.

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
| `vault/models.py`, `vault/migrations/0008_ledger_index_cleanup.py`, `0009_drop_reference_like_indexes.py` | 0008: drop four redundant indexes, ledger FK `db_index=False`; 0009: drop five reference `*_like` and two ledger `status` indexes (unread; 737 MB at 7.4 M accounts) |
| `vault/counts.py`, `views.py`, `templates/vault/pagination.html` | capped search totals and cached whole-table totals (opt-in / above 1 M rows), `N+` label |
| `vault/import_metrics.py`, `import_engine.py` | in-process import metrics (`vault.import.metrics`), no source values |
| `vault/importer.py`, `import_engine.py` (second pass) | all reference/account `INSERT ... SELECT` ordered by the unique key (deadlock fix); `refresh_stale_reference_stats` (runaway-plan fix) |
| `admin_LogIntel/settings.py` | `LOGGING` block (import metrics logger, console) |
| `benchmarks/*.sh`, `benchmarks/sql/` | ladder, experiment, crash, backup/PITR, partition scripts |
| `vault/tests*.py` | tests listed above |
| `README.md` | durability contract, fast mode vs online, counts and pagination contract, `recoverimports` |
| `SCALABILITY_ASSESSMENT.md`, `docs/*.md`, `benchmarks/**` | deliverables A-I, scripts and raw results |

The only settings change is the `LOGGING` block; no dependencies or `requirements.txt` entries were changed. No existing
public URL, form field or default search/pagination behaviour changed; the
cursor mode is a new opt-in query parameter. Tracked `__pycache__` files that
the test runs modified were restored.

## 3. Migrations

`0009_drop_reference_like_indexes`: forward 0.4 s, reverse 30.6 s on 8.3 M accounts; drops only indexes no code path reads.

`0008_ledger_index_cleanup` only drops indexes (see
`docs/DATABASE_OPTIMIZATION_REPORT.md` section 7). Forward 0.8 s, reverse 5.4 s on
a 1 M-row ledger; unique constraints unchanged; reversible; take a backup before
running on a very large database.

## 4. Configuration added

`VAULT_RESULT_COUNT_CAP`, `VAULT_TOTAL_COUNT_TTL`, `VAULT_TOTAL_COUNT_MIN_ROWS`, `VAULT_IMPORT_METRICS_INTERVAL` (defaults keep previous behaviour except cached totals above 1 M estimated rows), `VAULT_FAST_MODE_SCOPE` (`search` default, `all`), `importfolder --parallel-files`,
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
| Durable mode per its contract | **verified for crash and restart** | importer kill, `pg_ctl -m immediate` x2, PITR; power loss on a lying disk untested |
| No sacrifice of query availability for ingest speed | met | default is online mode; `--fast` narrowed and documented as not recommended |
| Indexes justified by measurement | met | two audits (0.88 M, 5.5 M); 0008 and 0009 |
| Exact and substring benchmarked separately | met | results sections 4 and 10 |
| Pagination/count contract defined | met | README + tests |
| Query latency measured during sustained ingestion | met to 7.4 M accounts, two workers | exact lookup p95 20-37 ms at every size; broad queries degrade (keyword p95 1.3-1.7 s at 5-7 M); no exports |
| Storage growth and index overhead quantified | met | storage tables |
| Progressive-scale tests show actual capacity | **partly met** | ladder to 8.27 M accounts; 10 M+ not run (disk); ingest falls to 30 % of the empty rate |
| Partitioning/sharding preserves uniqueness | **prototyped, not adopted** | single-node prototype showed no gain and `UNIQUE(id)` is impossible on a fingerprint-partitioned account table; sharding not built |
| Billion-record support | **not claimed**; unverified projections labelled |
| Automated tests pass; docs; security | met | 259 OK |

## 6. Known limitations and unresolved items

1. Scale: nothing above 8.27 M accounts was run; all numbers are from one
   2-vCPU sandbox with a fast virtual disk. Production hardware may change
   rankings (file parallelism, `wal_compression`, batch size, fsync cost).
2. Many measurements are single runs (spread 1-3 % where repeated twice).
   Latency percentiles with fewer than 50 samples (fast-mode variants) are
   indicative only.
3. The earlier `--fast` run recorded one `OperationalError` in a query thread; its
   message was not captured and it did not recur in later fast runs. Cause unknown
   (the deadlocks found later were between *import* transactions, not queries, so
   they are not an explanation).
4. The earlier ~30-minute stall of the opt-in performance class was **found and
   fixed** in the second pass: it was the account INSERT planned as a nested loop
   because a reference table's `reltuples` was 0, plus `deadlock detected` between
   `--parallel-files` jobs. Both are fixed (section 1); the fix was verified by 4
   runs of the failing sequence, not by an exhaustive search for other plan
   regressions. Other tables the importer touches (ledger, account) were not given
   the same guard.
5. `recover_job` reports outcome `partial` whenever lines were replayed, even if
   the job then completes; this is existing, tested behaviour and is now documented.
6. Uploaded (non-path) imports still cannot be replayed after the worker dies.
7. Legacy `ImportRecord.source_line` / `error_message` may hold plaintext for
   rows created before hash-only ledgers; not migrated.
8. The `perfbench concurrent` mix (7 queries, 50 ms think time, 2 workers) is
   synthetic; exports, concurrent jobs and autovacuum were not exercised.
9. Counts stay exact by default; the cap and cached totals are opt-in or only above
   1 M estimated rows, and the cache is per process.
10. Ledger retention (R6, archive-first) and sharding were not implemented.
11. Import metrics are logged, not exported to a monitoring system.
13. Keyword search through five trigram bitmaps (about 2.2 s idle at 8.3 M) is not
    improved.
14. Single runs only for the ladder, the drills and the experiments; the PITR drill
    used 1.8 GB, far below production sizes.
12. The README's older "Import speed" tables describe pre-existing measurements
    on different hardware and were not re-run.

## 7. Repository state

Changes are on branch `arena/b98bd230-credvault` and are committed there (see
`git log`). No other branch was touched. Large generated data (`/tmp/bench`) is
outside the repository.
