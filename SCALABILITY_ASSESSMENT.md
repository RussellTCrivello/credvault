# CredVault scalability assessment (deliverable A)

**Date:** 2026-10-19 (first pass 2026-10-18) · **Branch:** `arena/b98bd230-credvault` (from `main` @ `095abd0`)
**Scope:** repository and bottleneck assessment, updated after the implementation
program. Companion documents: `docs/PERFORMANCE_TARGETS.md`,
`docs/FILE_PROCESSING_ARCHITECTURE.md`, `docs/DATABASE_OPTIMIZATION_REPORT.md`,
`docs/BENCHMARK_RESULTS.md` (+ `benchmarks/`), `docs/CAPACITY_REPORT.md`,
`docs/OPERATIONS_AND_RECOVERY.md`, `docs/IMPLEMENTATION_AUDIT.md`.

## 1. Executive summary

CredVault is a Django + PostgreSQL application with a durable per-batch import
engine, per-line hash-only ledger, trigram substring search, and recovery by
lease/manifest. At the start of this program the repository's evidence covered
about 2.75 M accounts on a small VM and did not establish billion-record capacity
or fast search during ingestion.

What was done (all measured on a 2-vCPU, 3.9 GB sandbox with PostgreSQL 16.2; first
pass to 0.88 M accounts, second pass to **8.27 M accounts / 11.5 GB**):

* **Found with measurement, fixed:** a 17.4 % storage and 25 % import-time cost from
  four redundant ledger/account indexes; fast mode that suspended 31 indexes
  (not the documented five), was slower than online mode and made exact
  lookups 65x slower; an unbounded per-job reference cache (memory growth, plaintext
  credentials held for the whole job, not invalidated on rollback); unbounded line
  reads; a README/code mismatch on `synchronous_commit`; 91.8 % of pre-DB CPU in
  parsing, and a bounded host-normalization cache cut parse-stage time by 25 %.
* **Second pass (scale ladder, 0.88 M -> 8.27 M accounts):** durable ingest falls
  from about 6,050 to about 1,800 accounts/s as the data grows 9x (ingest, not
  search, bends first); exact lookups stay about 9 ms and p95 20-36 ms during
  imports; broad URL/keyword searches grow linearly (1.1 s / 2.2 s at 8.3 M). Added
  and measured: capped search totals and cached whole-table totals
  (1,095 -> 45 ms; 575 -> 34 ms), migration 0009 (seven more unread indexes,
  -737 MB, 7 %), import metrics, crash/PITR/backup drills (all exact), a
  partitioning prototype (no single-node gain). Two defects found by running the
  opt-in benchmarks and fixed: **deadlocks between parallel file imports**
  (unordered `INSERT ... ON CONFLICT` into the same unique index) and **a 25-minute
  runaway account INSERT** when a reference table's statistics said "empty".
  The decisions R1-R8 were delegated by the user and are recorded in
  `docs/PERFORMANCE_TARGETS.md`.
* **Still not verified:** 10 M+ accounts, billions of records, multi-node,
  replicas/failover, cold caches, a disk that loses acknowledged writes.
* **Added:** byte-aware bounded readers/queues/batches, importer resource limits,
  `--parallel-files`, `recoverimports` CLI, opt-in cursor pagination, the
  `perfbench` suite and `benchmarks/` scripts, and the documents above.
* **Evaluated and not adopted (evidence):** file-level parallelism by default
  (+29 % into an empty database, -4 to -11 % into a populated one, on 2 vCPU), intra-file byte-range segmentation (read/split is 2-3 % of
  the work), adaptive concurrency (no multi-core data), suspending indexes for
  incremental loads, partitioning/sharding (no shard key; global uniqueness).
* **Not established:** anything above ~1 M accounts, any billion-record claim,
  PostgreSQL-restart/power-loss/failover/backup drills, multi-core scaling,
  cold-cache behaviour. See `docs/CAPACITY_REPORT.md`.

## 2. Existing architecture (preserved)

Single PostgreSQL database; reference tables (`Url`, `Email`, `User`, `Pass`, `Ip`,
unique `name`), `Account` with unique `fingerprint` (sha256), `ImportJob` (lease,
manifest, counters), `ImportRecord` (per-line outcome, unique idempotency key).
Exact search uses btree on `lower(name)`; "domain" uses a `COLLATE "C"` range
index; Advanced search uses `pg_trgm` GIN on `UPPER(name)`. Views page ids first
and load rows for the page only. Public behaviour, uniqueness, search semantics
and exact counts are unchanged by default.

## 3. Status of the limits listed in the first assessment

| # | Original concern | Evidence gathered | Status |
| --- | --- | --- | --- |
| 1 | One database, no sharding | no tenant key; global uniqueness | **open**; design discussed, not built (`docs/DATABASE_OPTIMIZATION_REPORT.md` section 6) |
| 2 | Global uniqueness constraints | enforced in every test run (0 duplicate fingerprints, including concurrent file jobs and crash recovery) | **verified at small scale** |
| 3 | Ledger row per line | 783 B per line before, 527 B after; 30 % of storage | **reduced 33 %**; retention/partitioning needs approval (R6) |
| 4 | Fast mode suspends 31 indexes | slower than online mode and 5-70x worse reads on 880k accounts | **corrected**: default scope is the five trigram indexes; `all` is opt-in; online mode is the recommended path |
| 5 | Exact counts / page numbers | `COUNT(*)` 24-50 ms at 0.88 M (linear); `OFFSET` 44 ms vs keyset 0.1-0.3 ms at 50 % depth | **mitigated**: opt-in cursor mode; opt-in capped totals (`VAULT_RESULT_COUNT_CAP`) and cached whole-table totals; exact is the default; measured at 8.27 M (231 ms exact `count(*)`, 1,095 -> 45 ms capped search) |
| 6 | Broad substring cost profile | common-term queries 200-415 ms idle at 0.88 M; scale with the match set | **characterized to 8.27 M** (URL 1.1 s, keyword 2.2 s idle; keyword p95 1.3-1.7 s during imports), the count part removable with the cap, the match cost not; needs product limits or replicas |
| 7 | `work_mem` 1 GB | importer default now 64 MB, hard cap 256 MiB per node | **corrected**; production sizing still needed |
| 8 | `synchronous_commit` mismatch | durable path is `on` everywhere (test asserts); README claimed `off` in one section; compatibility loader (not production) uses `off` | **aligned** in docs, code and test; cost about 0.1 ms/commit in the sandbox |

## 4. Prioritized bottleneck table (measured)

| Rank | Bottleneck | Evidence | Remediation | Complexity / risk | State |
| ---: | --- | --- | --- | --- | --- |
| 1 | Parse/validate/normalize | 92 % of pre-DB wall; 12.5 CPU-us per line | host cache (-25 %); parse workers (existing) | low / low | done |
| 2 | Index and WAL cost per imported line | 6.2 KB WAL per line; 1.09 GB for 200 k lines | removed 4 redundant indexes (-26 % WAL); `wal_compression` option (-32 %, +11 % time) | low / low; compression is deployment choice | partly done |
| 3 | Fast mode misuse | 77-103 s vs 51 s online; reads degraded | scope narrowed, docs/benchmarks changed | low / low | done |
| 4 | Unbounded memory | cache grew with distinct values; 789 -> 637 MB at 1 M lines after bound; reader/queue bounds | byte-aware bounds everywhere | medium / low | done at 1 M lines |
| 5 | Offset pagination and exact counts | 44 -> 490 ms vs 0.1 ms (0.88 -> 8.27 M); linear counts | cursor mode, capped totals and cached totals, all opt-in/bounded | medium / low | done (opt-in) |
| 6 | Ledger growth | 30 % of bytes, unbounded | retention/partitioning proposal | high / needs approval | open |
| 7 | Broad substring queries during ingestion | 2-3x slower under load on 2 vCPU | none cheap; replicas or limits | high | open |
| 8 | Single-primary capacity | measured to 8.27 M; ingest 6,050 -> 1,800 acct/s | ladder stages 0-2b done; next stage needs more disk | high | open above 8.3 M |
| 9 | Ingest rate versus size | 3x loss from 0.9 M to 8 M; WAL 84 % full-page images | 0009 (-3 % WAL), tuning tried (`pglz` -50 % rate, `shared_buffers` 2 GB -18 %); needs RAM/hardware | high | open; first thing to bend |

## 5. Correctness and security risks found

| Risk | Resolution |
| --- | --- |
| Per-job reference cache not cleared on rollback: a retried batch could skip inserting reference values and silently lose accounts | cache cleared on any batch exception; test `test_failed_batch_clears_the_cache_before_the_error_propagates` |
| Reference cache kept plaintext credentials in memory for the whole job | bounded to 200,000 values per kind, cleared on overflow |
| Overlong lines allocate without bound | 16 KiB cap; hash-only audited outcome |
| Batch threshold counted skipped outcomes with valid rows, changing transaction boundaries (found by `CrashRecoveryTests`) | separate valid/outcome counters restored; test passes |
| Documentation said importer sets `off` while the durable path sets `on` | README fixed; only the compatibility loader uses `off` |
| `--fast` broader than documented; one query `OperationalError` seen with it | scope narrowed (the error message was not captured; not reproduced after) |
| Parallel file imports deadlocked on `vault_ip` (`deadlock detected`, job failed, recoverable): transactions inserted overlapping unique names in different orders | every reference and account `INSERT ... SELECT` is now `ORDER BY` the unique key; structural test `InsertOrderTests`; found by the opt-in `FileConcurrencyBenchmarkTests` (2 failures in 2 runs after the concurrent test; 0 in the verification runs of the fix: see `docs/BENCHMARK_RESULTS.md` section 22) |
| Account INSERT planned as a nested loop that rescans a 50,000-row table per staged row (one backend at 100 % CPU for 25 min) when `reltuples = 0` after an empty-table ANALYZE | `refresh_stale_reference_stats` analyzes only such tables inside the batch; tests `StaleStatisticsTests` |
| Perf tooling could leak data | synthetic only; prints no record contents; documented not to be run on production |

## 6. Open items and recommended next steps (by value)

1. Repeat stages 0-2 and continue to 10 M+ accounts on production-like hardware
   (more RAM, more cores, NVMe) with `benchmarks/scale_ladder.sh` and
   `perfbench concurrent`; this is where the ingest curve must be re-measured.
2. Still untested drills: disk full, power loss with a lying disk, replica
   failover, restore time at terabytes, `max_wal_size` above 4 GB.
3. Keyword search (five trigram bitmaps) is the slowest unaddressed query; needs
   a product limit, a different index design or replicas.
4. Ledger retention (R6) is recorded as archive-first but not implemented.
5. Counts and R1-R8 are decided (delegated; `docs/PERFORMANCE_TARGETS.md`).
6. Re-measure file-level parallelism, parse workers and `wal_compression` on a
   multi-core host; revisit intra-file segmentation only if reading is measured
   as a bottleneck on slow storage.
7. Evaluate dropping the five reference-table `*_like` indexes (about 135 MB
   per 0.88 M accounts) once ORM `startswith` usage is ruled out.
