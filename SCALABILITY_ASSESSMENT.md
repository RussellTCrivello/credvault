# CredVault scalability assessment (deliverable A)

**Date:** 2026-10-18 · **Branch:** `arena/b98bd230-credvault` (from `main` @ `095abd0`)
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

What was done (all measured on a 2-vCPU, 3.9 GB sandbox with PostgreSQL 16.2, up
to 0.88 M accounts plus 0.18 M imported during tests):

* **Found with measurement, fixed:** a 17.4 % storage and 25 % import-time cost from
  four redundant ledger/account indexes; fast mode that suspended 31 indexes
  (not the documented five), was slower than online mode and made exact
  lookups 65x slower; an unbounded per-job reference cache (memory growth, plaintext
  credentials held for the whole job, not invalidated on rollback); unbounded line
  reads; a README/code mismatch on `synchronous_commit`; 91.8 % of pre-DB CPU in
  parsing, and a bounded host-normalization cache cut parse-stage time by 25 %.
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
| 5 | Exact counts / page numbers | `COUNT(*)` 24-50 ms at 0.88 M (linear); `OFFSET` 44 ms vs keyset 0.1-0.3 ms at 50 % depth | **mitigated**: opt-in cursor mode; exact counts unchanged; cached-count policy needs approval (R5) |
| 6 | Broad substring cost profile | common-term queries 200-415 ms idle at 0.88 M; scale with the match set | **characterized**, not reduced; needs product limits or replicas |
| 7 | `work_mem` 1 GB | importer default now 64 MB, hard cap 256 MiB per node | **corrected**; production sizing still needed |
| 8 | `synchronous_commit` mismatch | durable path is `on` everywhere (test asserts); README claimed `off` in one section; compatibility loader (not production) uses `off` | **aligned** in docs, code and test; cost about 0.1 ms/commit in the sandbox |

## 4. Prioritized bottleneck table (measured)

| Rank | Bottleneck | Evidence | Remediation | Complexity / risk | State |
| ---: | --- | --- | --- | --- | --- |
| 1 | Parse/validate/normalize | 92 % of pre-DB wall; 12.5 CPU-us per line | host cache (-25 %); parse workers (existing) | low / low | done |
| 2 | Index and WAL cost per imported line | 6.2 KB WAL per line; 1.09 GB for 200 k lines | removed 4 redundant indexes (-26 % WAL); `wal_compression` option (-32 %, +11 % time) | low / low; compression is deployment choice | partly done |
| 3 | Fast mode misuse | 77-103 s vs 51 s online; reads degraded | scope narrowed, docs/benchmarks changed | low / low | done |
| 4 | Unbounded memory | cache grew with distinct values; 789 -> 637 MB at 1 M lines after bound; reader/queue bounds | byte-aware bounds everywhere | medium / low | done at 1 M lines |
| 5 | Offset pagination and exact counts | 44 ms vs 0.1-0.3 ms; linear counts | cursor mode (opt-in); count policy pending | medium / low | partly done |
| 6 | Ledger growth | 30 % of bytes, unbounded | retention/partitioning proposal | high / needs approval | open |
| 7 | Broad substring queries during ingestion | 2-3x slower under load on 2 vCPU | none cheap; replicas or limits | high | open |
| 8 | Single-primary capacity | unmeasured above 1.8 M | progressive tests (`docs/CAPACITY_REPORT.md` section 5) | high | open |

## 5. Correctness and security risks found

| Risk | Resolution |
| --- | --- |
| Per-job reference cache not cleared on rollback: a retried batch could skip inserting reference values and silently lose accounts | cache cleared on any batch exception; test `test_failed_batch_clears_the_cache_before_the_error_propagates` |
| Reference cache kept plaintext credentials in memory for the whole job | bounded to 200,000 values per kind, cleared on overflow |
| Overlong lines allocate without bound | 16 KiB cap; hash-only audited outcome |
| Batch threshold counted skipped outcomes with valid rows, changing transaction boundaries (found by `CrashRecoveryTests`) | separate valid/outcome counters restored; test passes |
| Documentation said importer sets `off` while the durable path sets `on` | README fixed; only the compatibility loader uses `off` |
| `--fast` broader than documented; one query `OperationalError` seen with it | scope narrowed (the error message was not captured; not reproduced after) |
| Perf tooling could leak data | synthetic only; prints no record contents; documented not to be run on production |

## 6. Open items and recommended next steps (by value)

1. Obtain approvals for R1-R8 (`docs/PERFORMANCE_TARGETS.md`).
2. Run stage 1 of the progressive plan (10 M accounts) on production-like
   hardware with `perfbench concurrent`; record WAL, vacuum and checkpoint logs.
3. Test PostgreSQL restart, power-loss-equivalent, disk-full, backup restore and
   failover drills against the durable engine.
4. Decide count freshness (R5) and, if approved, add a cached count with a label.
5. Decide ledger retention (R6) and design partitioning by job/time.
6. Re-measure file-level parallelism, parse workers and `wal_compression` on a
   multi-core host; revisit intra-file segmentation only if reading is measured
   as a bottleneck on slow storage.
7. Evaluate dropping the five reference-table `*_like` indexes (about 135 MB
   per 0.88 M accounts) once ORM `startswith` usage is ruled out.
