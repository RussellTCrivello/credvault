# Operations and recovery (deliverable H)

## 1. Settings

| Setting | Default | Effect | Evidence / caution |
| --- | --- | --- | --- |
| `VAULT_IMPORT_WORKERS` | `min(4, CPUs)` (max 16) | parse processes per job | parsing is 92 % of pre-DB CPU time; do not exceed `cores - 1` |
| `VAULT_IMPORT_BATCH_ROWS` | 250,000 (max 1,000,000) | rows per commit | 250 k was fastest of 10 k/50 k/250 k; memory about 2.5 KB per buffered row; a crash recomputes up to one batch |
| `VAULT_IMPORT_WORK_MEM` | 64MB (max 256MB) | `SET LOCAL work_mem` for import transactions | `work_mem` applies per sort/hash node and per session; size against concurrent sessions |
| `VAULT_MAINTENANCE_WORK_MEM` | 1024MB | trigram index rebuild after `--fast` | |
| `VAULT_FAST_MODE_SCOPE` | `search` | `search`: only the five trigram indexes are suspended; `all`: every non-unique btree index too | `--fast` was slower than online mode and degraded reads in the measured regime; `all` made exact lookups 65x slower |
| `importfolder --parallel-files N` | 1 (max 8) | independent file jobs, one parser process each | +29 % into an empty database, -4 to -11 % into a populated one on 2 vCPU; measure on your workload first |
| `VAULT_IMPORT_METRICS_INTERVAL` | 30 s (0 disables) | seconds between `vault.import.metrics` log lines | counts and timings only; `db_busy_ratio` near 1 means database-bound, low means the parser or reader starves it |
| `VAULT_RESULT_COUNT_CAP` | 0 (exact) | cap on search totals (`N+` shown) | measured 1,095 -> 45 ms for a broad search at 8.27 M; recommended above about 5 M accounts if exact totals are not required |
| `VAULT_TOTAL_COUNT_TTL` | 60 s (0 = live) | cache of whole-table totals for tables of at least `VAULT_TOTAL_COUNT_MIN_ROWS` (1,000,000) estimated rows | exact when counted, at most TTL old; per process (use a shared Django cache for several workers) |
| `importfolder --fast` | off | see above | prefer the default online mode for incremental loads |

Also set at the PostgreSQL level (not changed by the code): `shared_buffers`,
`effective_cache_size`, `max_wal_size`, `checkpoint_timeout`,
`wal_compression` (`pglz`: -32 % WAL and +11 % time at 0.88 M, but -25 % WAL and **-50 % rate** at 8 M on 2 vCPU: do not enable on a 2-core host), `autovacuum`.
Do **not** copy a 1 GB `work_mem` from any local setup.

## 2. Running and watching an import

```bash
python manage.py importfolder /data/in --ext '*'          # online, durable (default)
python manage.py importfolder /data/in --parallel-files 4  # only after measuring
```

* Progress and per-job counters: Import jobs page (`/import/jobs/`) and job
  detail page (status, stage, committed/skipped/failed, recovery log).
* Database side: `select * from pg_stat_activity where state <> 'idle'`;
  `pg_stat_progress_copy`; WAL growth `select wal_bytes from pg_stat_wal`;
  checkpoint pressure in the server log.
* Queue depth, worker utilization and DB latency are **not** exported as
  metrics today. Use the job page, `perfbench concurrent` and PostgreSQL views.
* Pause, Continue and Stop act at a durable batch boundary.

## 3. Recovering failed or interrupted imports

```bash
python manage.py recoverimports            # list abandoned, replayable jobs
python manage.py recoverimports --run      # recover them
python manage.py recoverimports --run --job <UUID>
```

* Same logic as the Resume button: reconciles the ledger, validates the
  source manifest and replays only positions without a terminal outcome.
* A job whose lease is still live (renewed every 100 s, 300 s expiry) is not
  touched; wait for the lease to expire or stop the worker first.
* A source file that changed since the manifest is refused: restore the
  original or start a new import (new job, new positions).
* A recovery log outcome `partial` means "lines were replayed", not failure;
  `failed` and `completed_with_errors` need inspection; `success` means nothing
  needed replay.
* A browser that leaves the import page no longer fails an upload import (it did
  before 2026-10-19: Django deleted the temporary upload while the worker still
  read it, the job failed with `SourceChangedError` and a log line
  `Import source changed for job ... (SourceChangedError: An uploaded source became
  unavailable while it was read; cause ValueError)`). The log line now carries the
  fixed message and the cause type, never a source value. A job that failed this
  way keeps its committed lines; re-upload the file (idempotent for accounts) or,
  better for millions of lines, use `importfolder`.
* Uploaded (non-path) imports cannot be replayed after the worker dies; upload
  again (global fingerprint uniqueness makes the re-upload idempotent for
  accounts, but it creates a new job and ledger).

What a crash loses: only the current uncommitted batch, which recovery
recomputes. Verified by killing the process (SIGKILL) at 81,582 durable lines
of 200,000 and recovering (`docs/BENCHMARK_RESULTS.md` section 9). **Not verified:**
PostgreSQL crash, host/power loss, full disk, backup restore, replica
failover. Run those drills before relying on the guarantee.

## 4. Interrupted fast mode

If a process is killed while indexes are suspended, the next fast import (or
`ensure_search_indexes()`) recreates all missing indexes first. Until then,
substring searches (default scope) or most lookups (scope `all`) are slow.
To repair by hand: `python manage.py shell -c "from vault.importer import ensure_search_indexes; ensure_search_indexes()"`.

## 5. Retention and audit

Per-line ledger rows (hash-only, no plaintext for new imports) are never
deleted or archived by any code in this repository. They are 30 % of
storage (527 bytes per source line after migration 0008). Any retention policy
needs an approved requirement (R6) and a migration; until then plan storage for
the full ledger. Legacy rows may still hold plaintext `source_line` /
`error_message` (admin hides them); treat those columns as secrets.

## 6. Diagnosing degradation

| Symptom | Check |
| --- | --- |
| Searches slow during an import | Was `--fast` used? (`VAULT_FAST_MODE_SCOPE=all`?) Check indexes: `select indexname from pg_indexes where tablename like 'vault_%'`; compare with `perfbench storage` |
| Import slower than before | `perfbench stages` for parse speed; `pg_stat_wal` and checkpoint messages; autovacuum workers; `perfbench storage` for index bloat |
| Importer memory high | batch rows, parse workers, concurrent file jobs; see budgets in `docs/PERFORMANCE_TARGETS.md` |
| `COUNT(*)` slow on dashboards | linear growth; ask for the cached-count decision (R5); cursor mode avoids totals |
| Deep pages slow | use cursor mode (`?sort=id&after=<id>`) |
| Job stuck in `processing` | lease expired? `recoverimports` lists it |

## 7. Security handling

* The benchmark tooling generates synthetic data only, prints no record contents
  and was run against a disposable database; keep it that way (do not point
  `perfbench` at production, and do not paste real records into reports).
* Importer buffers and parse queues hold plaintext credentials in process
  memory only for the life of a batch; the reference cache is bounded and cleared
  on rollback. Staging uses session-local temp tables, which are not persisted.
* No new persistent copy of credentials was introduced (no external index, cache,
  queue or replica). Any future one must be included in the threat model and
  inherit access controls and retention (see `docs/CAPACITY_REPORT.md`).
* Run PostgreSQL with a least-privilege application role (`CREATEDB` only on
  test hosts), protect WAL archives/backups like the primary data, and keep
  the `perfbench` scratch directory (`/tmp/bench` by default) on protected
  storage; it contains synthetic files only.

## 8. Rollback of this program's schema change

`python manage.py migrate vault 0007` recreates the four dropped indexes
(about 5 s per million ledger lines; full scans). Back up first on a large
database. Code compatibility: the code works with or without 0008 (the
indexes are only an optimization).

## 9. Drills (run them before relying on this)

All drills are scripts in `benchmarks/`, run against a disposable cluster; results in
`docs/BENCHMARK_RESULTS.md` sections 9, 17 and 18.

| Drill | Script | What it proves | What it does not prove |
| --- | --- | --- | --- |
| Importer killed (SIGKILL) | `kill_recover.sh` | only uncommitted positions replay; counts equal an uninterrupted run | |
| PostgreSQL stopped with `-m immediate` mid-import | `pg_crash_drill.sh DIR EXPECTED_ACCOUNTS EXPECTED_LINES` | server WAL redo, `pg_amcheck --heapallindexed` clean, `recoverimports --run` completes the job exactly | power loss with a disk that loses acknowledged writes |
| Physical backup, accidental delete, PITR | `backup_restore_drill.sh MORE_DATA_DIR` | `pg_basebackup` + WAL archive restores to the second before the error with exact counts and clean `amcheck` | restore time at terabytes, off-site copies, encryption of the backup |

Restore procedure for production: take the backup with `pg_basebackup -X stream`,
archive WAL (`archive_mode=on`), restore into a **new** directory with
`recovery.signal`, `restore_command` and `recovery_target_time`, and **wait for
`pg_is_in_recovery()` to be false** (read-only connections open while WAL is still
being replayed). Backups and the WAL archive contain every credential: protect
them like the database (R7).

## 10. Reading the import metrics

`grep import_metrics` the application log. Interpretation, from measured runs: an
empty-database import shows `db_busy_ratio` about 0.9 and a parse backlog of about
10 MB (the bound); a falling `items_per_second` with `db_busy_ratio` near 1 is the
database (index size, WAL); a low `db_busy_ratio` with an empty backlog is the
reader; a low ratio with a full backlog would be the commit path. Metrics are
in-process and not exported to a monitoring system.

## 11. Rollback of migration 0009

`manage.py migrate vault 0008` recreates the seven indexes (30.6 s at 8.3 M accounts,
table scans). The five reference `_like` indexes and the two ledger `status` indexes
have no reader in the code; nothing else changes.
