# CredVault benchmark suite

Everything here runs against a **disposable** PostgreSQL cluster. Nothing is
executed by the normal test suite. Results from 2026-10-18 are in
`results/2026-10-18/` and `results/2026-10-19/` (raw JSON/text, no record contents; the second set is the scale ladder to 8.27 M accounts, index audit, counts, drills and prototype).

## Environment used for the recorded results

2 vCPU, 3.9 GB RAM, one virtual disk, PostgreSQL 16.2 over a Unix socket
(`shared_buffers=512MB`, `max_wal_size=4GB`, `wal_compression=off`,
`fsync=on`, `synchronous_commit=on`), Python 3.11.2, Django 5.2.18.
Absolute numbers are specific to that sandbox; compare variants only inside
one run set.

## Building blocks (all `manage.py perfbench ...`, JSON on stdout)

| Command | Measures |
| --- | --- |
| `perfbench generate DIR --lines N --files F --duplicate-rate R --invalid-rate R --seed S` | deterministic synthetic files (skewed hosts, duplicates, malformed lines) |
| `perfbench stages FILE --chunk-bytes 4096 65536 1048576` | per-stage wall/CPU: raw read, line split, per-line SHA-256, decode, parse+validate+normalize |
| `perfbench queries --runs 7 --explain` | p50/p95 of the real views (exact, domain, rare/common substring, deep page, count) and `EXPLAIN (ANALYZE, BUFFERS)` of the underlying statements |
| `perfbench storage` | bytes per table and per index, bytes per account |
| `perfbench counts` | cost of exact vs bounded vs cached counts at the current table size |
| `perfbench concurrent PATH [--fast] --query-workers N` | idle vs during-import latency (p50/p95/p99/max) with a durable import running, plus accounts/s, WAL bytes, database growth |

## Reproducing the experiments

```bash
export CV_ENV_FILE=/path/to/env.sh   # exports POSTGRES_HOST/USER/PASSWORD/DB
# 1. load a base data set (database `credvault`), then VACUUM (ANALYZE)
manage.py perfbench generate /data/g1m --lines 1000000 --files 8 \
    --duplicate-rate 0.1 --invalid-rate 0.02 --seed 7
manage.py importfolder /data/g1m
# 2. every variant below clones that database (CREATE DATABASE ... TEMPLATE) so
#    each run starts from identical data
benchmarks/run_variant.sh online /data/g200k --query-workers 2
MIGRATE=1 benchmarks/run_variant.sh fast_search /data/g200k --fast --query-workers 2
benchmarks/run_batch.sh batch_250000 VAULT_IMPORT_BATCH_ROWS=250000 \
    .venv/bin/python manage.py importfolder /data/g200k
benchmarks/run_batch.sh files4 VAULT_IMPORT_BATCH_ROWS=250000 \
    .venv/bin/python manage.py importfolder /data/g200k --parallel-files 4
benchmarks/kill_recover.sh        # SIGKILL mid-import, recover, verify
benchmarks/run_mem.sh             # peak RSS of the process tree, 200k vs 1M lines
```

Scripts assume the pgserver wheel layout for `psql`; edit `PGB`/`PSQL` for a
system PostgreSQL. `CV_ROOT` and `CV_BENCH` override the repo and scratch
directories. Keep each run's JSON next to the commit it was taken at.

## Scale ladder, experiments and drills (results/2026-10-19)

| Script | Purpose |
| --- | --- |
| `scale_ladder.sh` | grows the database stage by stage (`s0` 1 M lines, `s1` +2 M, `s2` +3 M), recording import time, sizes, WAL, `perfbench queries`/`storage` and a concurrent run per stage |
| `exp_import.sh` | one cloned-database import experiment with a set of environment overrides (used for the index, WAL, `shared_buffers` and fast-mode comparisons) |
| `pg_crash_drill.sh DIR EXPECTED_ACCOUNTS EXPECTED_LINES` | stop PostgreSQL with `-m immediate` mid-import, restart, `pg_amcheck`, `recoverimports --run`, compare with an uninterrupted run (`KILL_AFTER` seconds, `CV_PGDATA`) |
| `backup_restore_drill.sh DIR` | `pg_basebackup` + WAL archive, import, delete rows, restore to just before the delete, wait for recovery to end, compare counts, `pg_amcheck` |
| `sql/partition_prototype.sql` | hash-partitioning prototype of the ledger and account tables, 1 M rows |

`pg_trgm` and `amcheck` must exist in the cluster (`CREATE EXTENSION`); some
minimal PostgreSQL bundles ship without contrib modules.

## Opt-in tests (inside the Django test runner)

```bash
VAULT_RUN_PERFORMANCE=1 VAULT_PERF_LINES=200000 manage.py test tests.tests_performance.ImportThroughputTests
VAULT_RUN_CONCURRENT_PERF=1 manage.py test tests.tests_performance.ConcurrentImportSearchTests
VAULT_RUN_FILE_CONCURRENCY_PERF=1 manage.py test tests.tests_performance.FileConcurrencyBenchmarkTests
```

These use tiny result sets and an empty database; the `perfbench concurrent`
flow above is the representative one (populated database, broad queries).

## Regression budgets (proposed, not enforced in CI)

Timing gates are only meaningful on a fixed, otherwise idle machine, so CI runs
correctness tests only. On the reference machine, re-run `run_variant.sh` for
the online variant and investigate when, versus the stored results: accounts/s
drops by more than 15 %, `exact_email` during-import p95 exceeds 100 ms,
bytes per account grows by more than 10 %, or peak RSS grows by more than 25 %.
