# Performance targets, budgets and decisions (deliverable B)

> **Provenance.** The first commit of this branch contained this file empty
> (a defect found and fixed in the follow-up session). It is rewritten here from
> the measurements in `docs/scalability/BENCHMARK_RESULTS.md`. The numbering R1-R8 below is
> re-established; where earlier documents cite R5, R6 or R8 the meaning is
> unchanged (count freshness, ledger retention, shard key).

## 1. What the owner asked for

* Very fast ingestion and querying from millions toward billions of records,
  shown end to end, not as isolated micro-benchmarks.
* Interactive searches stay fast **while** a bulk import runs.
* No weaker durability, no plaintext copies of credentials in queues, caches,
  indexes, logs or replicas, no silent loss of audit data, no silent change of
  existing semantics.
* Measured results, projections and untested requirements kept apart.

## 2. Working targets and where they stand

Hardware-relative: every figure below was measured on **2 vCPU, 3.9 GB, one
virtual disk, PostgreSQL 16.2** (`docs/scalability/BENCHMARK_SUITE.md`). Treat the targets as
shapes to re-verify on production hardware, not as promises.

| # | Target | Measured status | Verdict |
| --- | --- | --- | --- |
| T1 | Exact lookup (email / domain-rare) p95 <= 100 ms during an import, independent of data size | p95 20-36 ms (email) and 64-72 ms (rare domain) at 2.6 M, 5.5 M and 7.4 M accounts, 2 query workers; idle p50 about 9 ms at 0.88 M and 8.27 M | **met up to 8.3 M** |
| T2 | Broad first page (domain, URL substring) p95 <= 1 s during an import | exact totals: p95 363 -> 534 -> (7.4 M not run exact); with `VAULT_RESULT_COUNT_CAP=100000` at 7.4 M: p95 147-194 ms | **met only with the cap**; exact totals grow linearly with matches |
| T3 | Keyword search (OR over five tables) p95 <= 1 s during an import | p95 565 ms (2.6 M), 1,694 ms (5.5 M), 1,281 ms (7.4 M, capped); idle 8.3 M: 2,154 ms | **not met** at >= 5 M; cost is the trigram bitmap on a broad pattern, not the count (section 5 of `docs/scalability/BENCHMARK_RESULTS.md`) |
| T4 | Whole-table totals (dashboard, Records, References) <= 100 ms | 575 ms -> 34 ms (dashboard), 189 -> 3.5 ms (Records), 545 -> 32 ms (References) at 7.4 M with the cached total | **met** with the default TTL |
| T5 | Durable online ingest, empty to 1 M lines | 6,050 accounts/s (145 s) | **met** (reference) |
| T6 | Durable online ingest at >= 5 M accounts, no query load | 1,714-1,835 accounts/s at 7.6-8.1 M | recorded; see T7 |
| T7 | Durable online ingest with two query workers at >= 5 M accounts >= 1,000 accounts/s | 1,753 (2.6 M), 1,019 (5.5 M), 870 (7.4 M, cap on) | **met to 5.5 M, narrowly missed at 7.4 M**; import rate falls steadily with size (about 3x from 0.9 M to 8 M accounts), I/O-bound on this host |
| T8 | Importer memory <= 1 GB RSS (process tree) at 1 M lines | 637 MB single process, 735 MB tree | **met** |
| T9 | Crash/kill: no lost, duplicated or phantom record; resume replays only uncommitted positions | SIGKILL of the importer (2 runs) and `pg_ctl stop -m immediate` of the server (2 runs): final counts identical to an uninterrupted run, 0 duplicate fingerprints, `pg_amcheck` clean | **met** for process and server crash; **not** tested: power loss with a lying disk |
| T10 | Point-in-time recovery from a physical backup, exact to a chosen time | 1.8 GB backup, WAL replay to the second before an accidental delete: row counts identical, 0 duplicates, amcheck clean (15 s) | **met** (single run, small data) |
| T11 | No search index dropped for a normal incremental import | default online mode; `--fast` was 1.8-2x slower than online at 0.88 M and degraded reads | **met** by default |
| T12 | Billion-record capacity | not run; projections only | **not claimed** |

## 3. Resource budgets (enforced in code)

| Resource | Budget | Where |
| --- | --- | --- |
| Parse workers | `min(4, CPUs)` on Linux/macOS, 1 on Windows, max 16, `VAULT_IMPORT_WORKERS` | `importer.worker_count` |
| In-flight parse bytes | bounded queue plus byte cap | `MAX_IN_FLIGHT_PARSE_BYTES` |
| Commit batch | 250,000 rows or a byte cap, whichever first | `VAULT_IMPORT_BATCH_ROWS`, `MAX_IMPORT_BUFFER_BYTES` |
| Reference-value cache | 200,000 values, cleared on rollback | `MAX_REFERENCE_CACHE_VALUES` |
| `work_mem` for import transactions | 64 MB default, 256 MB cap | `VAULT_IMPORT_WORK_MEM` |
| Source line | 16 KiB, longer lines hash-only | `utils.MAX_SOURCE_LINE_BYTES` |
| Concurrent file jobs | 1 default, max 8 | `importfolder --parallel-files` |
| Search total | exact by default, optional cap | `VAULT_RESULT_COUNT_CAP` |
| Whole-table totals | cached 60 s above 1 M estimated rows | `VAULT_TOTAL_COUNT_TTL`, `VAULT_TOTAL_COUNT_MIN_ROWS` |

## 4. Decisions R1-R8

The owner delegated these decisions ("I agree to whatever you deem appropriate,
complete, and reliable"). Each row records what was decided, why, and how to
reverse it. They are working decisions of the engineer, not independent
sign-off; a production owner should re-confirm R5, R6 and R8 in particular.

| # | Decision | Reason | Reversal |
| --- | --- | --- | --- |
| R1 | Adopt T1-T12 as the working targets, hardware-relative | no other numbers exist; each has a measurement behind it | edit this file |
| R2 | Durability contract unchanged: `synchronous_commit=on` for import commits, loss bounded to the uncommitted batch, recovery by the ledger | the owner forbade weaker durability | none needed |
| R3 | Online mode stays the default; `--fast` stays opt-in and documented as not recommended for incremental loads | measured slower and read-degrading (section 16 of the results) | none |
| R4 | The budgets in section 3 are the importer's resource limits | measured; prevent unbounded memory and connection growth | environment variables |
| R5 | Count freshness: search totals are exact unless the operator sets `VAULT_RESULT_COUNT_CAP`; whole-table totals are exact below 1 M estimated rows and exact-as-of at most 60 s ago above | the exact count is the whole cost of a broad search (1-2 s at 7.4 M versus 2 ms for the page); a cap is visible (`N+` label and note), cached totals refresh on every UI add/delete | `VAULT_RESULT_COUNT_CAP=0` (default), `VAULT_TOTAL_COUNT_TTL=0` |
| R6 | Ledger retention: **no deletion is implemented**. Partitioning or archiving the ledger needs an archive-first design because the ledger is the audit trail and the exactly-once guarantee | the owner forbade loss of audit data; the ledger is 40 % of table bytes at 8.3 M accounts | n/a |
| R7 | Security: metrics and logs carry counts, durations and job UUIDs only; no queue, cache, replica, backup or metric may hold plaintext credentials beyond the database itself, and every copy needs the database's access controls | standing requirement | n/a |
| R8 | No partitioning or sharding is adopted. Revisit only when a measured trigger fires: (a) a single table's maintenance window exceeds its budget, (b) the hot index set no longer fits RAM and lookups leave T1, or (c) disk exceeds one volume. A shard key would have to be invented (the product has none) | prototype (section 19 of the results): no insert, lookup, count or size gain on one node; partitioning `vault_account` breaks the ledger foreign key | n/a |
