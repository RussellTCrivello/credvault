# Decision record, risk register and continuation

The single authoritative record of what was decided, why, what is still open, and
where to resume. Evidence lives in `docs/scalability/BENCHMARK_RESULTS.md` (section
numbers below); this file does not repeat the measurements.

Status words: **verified** = run and observed; **field** = from the user's own log, not
measured by the engineering agent; **projection** = estimate, not measured.

## 1. Decisions

| # | Decision | Evidence | Alternatives rejected | Status |
| --- | --- | --- | --- | --- |
| D1 | Keep the durable, per-line-audited import engine; optimize inside it (bounded readers, byte-aware batches, ordered inserts) | Baseline profile: parsing ~92 % of CPU; ledger gives resume and verify | Replace with a COPY-only loader (loses per-line audit and resume) | verified |
| D2 | Drop 4 + 7 secondary indexes (migrations 0008, 0009, both reversible) | -17.4 % storage / -25 % import time; -737 MB at 7.4 M | Keep all indexes (slower imports, more disk) | verified |
| D3 | `--fast` stays opt-in and not recommended | slower than online on a populated DB and hurts query latency (section 14) | Make it the default | verified |
| D4 | File parallelism (`--parallel-files`) stays opt-in, default 1 | +29 % on an empty DB, -4 to -11 % on a populated one | Default to N files | verified |
| D5 | `ORDER BY` on every reference/account `INSERT ... SELECT` | deadlocks between parallel files; structural guard test | Retry on deadlock only | verified |
| D6 | Refresh stale reference-table statistics before large inserts | 25-minute runaway INSERT from `reltuples=0` | Rely on autovacuum | verified for reference tables only |
| D7 | Opt-in keyset pagination, capped/cached counts | OFFSET 44 -> 490 ms vs keyset ~0.1 ms; counts 1,095 -> 45 ms at 8.27 M | Exact `count(*)` always | verified |
| D8 | No partitioning, no sharding on one node | prototype gave no gain; `UNIQUE(id)` impossible on a fingerprint-partitioned table | Partition accounts | verified (single node) |
| D9 | Upload imports: a client disconnect must not abort the job | field failure `Import source changed; resume refused.`; fixed with a failing-then-passing test | Leave as is | verified (test), field re-run not done |
| D10 | Windows: keep the code default of one parse worker; document `VAULT_IMPORT_WORKERS=3` | field `db_busy_ratio` 0.81 on one worker, 0.90 on three | Change the default blind | field |
| D11 | **Read-only PostgreSQL settings advisory at import start** (`vault/db_advisor.py`) | field: stock settings ~6,900 lines/s vs tuned ~12,000-13,000 lines/s; four manual round trips were needed to find it | Docs only (already existed and was not applied); auto-`ALTER SYSTEM` (needs superuser and restart, silent server change) | verified (14 tests; real runs on stock and tuned servers) |
| D12 | Warn only on `shared_buffers` and `max_wal_size` at or below stock; display but do not warn on `checkpoint_timeout` | `checkpoint_timeout=30min` had no effect in the sandbox and was not isolated in the field | Warn on all three | verified |
| D13 | **Do not** skip ledger rows for blank lines | sandbox: 64.5 % blank input added 27 % run time; skipping changes audit, resume and verify semantics | Implement it (needs the owner's approval and resume/verify tests); pre-filter blank lines outside the importer instead | decided, awaiting owner if wanted |
| D14 | Documentation lives in `docs/`, tests in `tests/` | owner request | - | verified |

## 2. Risk register

| Risk | Likelihood / impact | Evidence | Mitigation | Status / remaining exposure |
| --- | --- | --- | --- | --- |
| PostgreSQL on stock settings halves import speed | High on fresh installs / high | field run (section 24) | advisory (D11), README tuning steps | mitigated by warning; applying the settings is the operator's action |
| Rate keeps falling as the database grows | High / medium | sandbox ladder 6,050 -> 1,800 accounts/s (0.9 M -> 8 M); field 14,480 -> 11,969 lines/s over 6 M lines | none beyond tuning | not isolated; unmeasured above 8.3 M accounts |
| Keyword and common-URL search exceed 1 s at >= 5 M accounts | High / medium | sandbox idle p50 1,534 ms keyword at 5.46 M | none implemented | open (target T3 not met) |
| No evidence for billion-record operation | Certain / high | projections only (`CAPACITY_REPORT.md` section 3) | none | open; no claim is made |
| Ledger row per blank/skipped line adds disk and time | Medium / medium | 27 % in sandbox test | pre-filter, or D13 if approved | open |
| ~9 % of non-blank lines are rejected by the parser for reasons not yet examined | Unknown / unknown | `explainrejects` sample: mostly `:`-separated lines failing the URL or password/IP check | needs masked sample lines from the owner | open |
| Stale-statistics guard covers reference tables only | Low-medium / high when it hits | D6 | extend to ledger/account tables | open |
| Browser upload cannot resume after a server restart | Medium / medium | by design | use `importfolder` + `recoverimports` | accepted |
| Sandbox results do not transfer to other hardware | Certain / medium | 2 vCPU, 3.9 GB | measure on target hardware | accepted; stated everywhere |

## 3. Open tasks (priority order)

1. Owner: press **Verify** on job `01d566e4-65fe-4808-a246-d395ae7d8f74` and report the result
   (the completed 18.2 M-line run has not been verified by the ledger check).
2. Owner: run `ANALYZE;` once after the bulk load.
3. Owner: send a few **masked** rejected lines (from `manage.py explainrejects`) to decide
   whether the parser is correctly strict.
4. Optional, needs approval: D13 (count blank lines without a ledger row).
5. Optional: extend the stale-statistics guard to the ledger and account tables.
6. Optional: measure keyword search after the 3.84 M-account load on the owner's hardware.

## 4. Continuation point (engineering state)

* Branch `arena/b98bd230-credvault`, PR #12, base `main` @ `095abd0`. Last pushed commit
  before this change: `35943c5`.
* Reproduce the test environment after a sandbox reset: `python3 -m venv .venv`;
  `.venv/bin/pip install -r requirements.txt pgserver==0.1.4`; compile `pg_trgm` from the
  PostgreSQL REL_16_2 contrib sources with PGXS (`USE_PGXS=1`); `initdb`, start a local
  cluster, `createdb credvault`; export `POSTGRES_HOST/USER/PASSWORD/DB`; then
  `python manage.py test --noinput` (278 tests, 5 skipped, about 85 s).
* Opt-in performance tests and the benchmark scripts are described in
  `docs/scalability/BENCHMARK_SUITE.md`.
