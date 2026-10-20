# Database optimization report (deliverable D)

Measured data: `docs/scalability/BENCHMARK_RESULTS.md`, `benchmarks/results/2026-10-18/`.
Base: 879,519 accounts, 1,000,000 ledger rows, PostgreSQL 16.2.

## 1. Storage per record (measured)

| Component | MB (before 0008) | Bytes per account | After 0008 (MB) |
| --- | ---: | ---: | ---: |
| `vault_importrecord` (ledger, 1 M rows) | 782.8 (heap 300.4 + indexes 482.3) | 890 (783 per source line) | 527.0 |
| `vault_account` | 509.6 (heap 150.2 + indexes 359.4) | 579 | 399.3 |
| `vault_pass` | 274.4 | 312 | 274.4 |
| `vault_email` | 262.2 | 298 | 262.2 |
| `vault_ip` | 137.9 | 157 | 137.9 |
| `vault_url` | 97.7 | 111 | 97.7 |
| `vault_user` | 34.1 | 39 | 34.1 |
| **Total** | **2,098.9** | **2,386** | **1,732.8 (1,970 per account)** |

Without the ledger, 1,316 MB / 879,519 = 1,497 bytes per account, which matches
the repository's earlier "about 1.5 GB per million" figure for its data mix;
the ledger adds 783 bytes per source line before 0008 and 527 after. WAL is
extra: 1.09 GB for a 200k-line import (6.2 KB per line, mostly first-touch page
images of the index pages), 0.74 GB with `wal_compression=pglz`.
Replicas, backups and headroom are not measured; plan at least 2x for a replica
plus backups.

**Projection, not a measurement:** at the measured per-row costs, 1 B unique
accounts with a full ledger would need roughly 2.0 TB of tables and indexes
(1,970 B x 1 B) before WAL, bloat, replicas and backups; real sizes grow with
index depth and value cardinality. See `docs/scalability/CAPACITY_REPORT.md`.

## 2. Index audit (every index on the ledger, account and reference tables, 880k accounts)

*Update:* the same audit was repeated at 5.5 M accounts
(`docs/scalability/BENCHMARK_RESULTS.md` section 13). It confirmed 0008, found seven more
unread indexes (five reference `*_like`, two ledger `status`), which migration
0009 drops, and showed the `lower(name)` indexes are read by `_match_exact_ids` and
must stay. The table below is the original 880 k audit.

Index sizes in MB; "scans" are `pg_stat_user_indexes` counts accumulated during
the benchmark workload and are only a hint.

| Table | Index | MB | Decision |
| --- | --- | ---: | --- |
| importrecord | `idempotency_key_key` (unique) | 124.7 | keep: idempotency authority |
| importrecord | `vault_importrec_idem_idx` | 124.7 | **dropped**: exact duplicate of the unique index |
| importrecord | `idempotency_key_da77985c_like` | 124.7 | **dropped**: `varchar_pattern_ops`, only for `LIKE 'x%'`; code does equality/`IN` only |
| importrecord | `import_job_id_8be0a716` | 6.4 | **dropped**: prefix of both composite indexes |
| importrecord | `job_pos_idx (import_job_id, source_position)` | 30.9 | keep: recovery gap scan, ledger page |
| importrecord | `job_status_idx (import_job_id, status)` | 7.2 | keep: failed-row retrieval per job |
| importrecord | `status`, `status_like` | 7.1 + 7.1 | keep for now: admin filter; `_like` is a drop candidate (7 MB per M lines, unmeasured benefit) |
| importrecord | `account_id` | 26.9 | keep: FK `SET NULL` on account delete needs it |
| account | `fingerprint_uniq` (unique) | 110.3 | keep: global uniqueness authority |
| account | `fingerprint_like` | 110.3 | **dropped**: pattern index on a hex digest, equality only |
| account | `url_id`, `email_id`, `user_id`, `password_id`, `ip_id` | 13.5-26.8 each | keep: every search joins accounts by these |
| account | `created_id_idx (created_at DESC, id)` | 27.8 | keep: default "newest" sort; not used by the id-ordered cursor mode |
| reference tables | `name` unique, `lower(name)`, `lower(name) COLLATE "C"` (url), `upper(name)` trigram | 5-51 each | keep: exact (B-tree), domain range and `contains` (trigram) searches |
| reference tables | `name_*_like` (5 tables, 5-47 MB each, about 135 MB) | | **candidate, not dropped:** no startswith/LIKE-prefix in the repository, but admin and ORM `startswith` lookups would use them; needs a query-plan check with product owners |

Net change: -366 MB (-17.4 %) at 1 M ledger lines, 25 % faster online import,
26 % less WAL (migration `0008_ledger_index_cleanup`; forward 0.8 s, reverse
5.4 s on this data). The drop list is repeated in `vault/importer.py`
(`_FAST_MODE_INDEXES`) so `--fast` cannot recreate dropped indexes; a test asserts
the schema.

Not adopted:

* **BRIN** on `created_at`/`id`: the table is insert-ordered, so BRIN would be tiny
  (kilobytes) but the planner uses the btree for `ORDER BY created_at DESC` and
  `id` keyset today; no workload needing a BRIN scan was identified.
* **Partial/covering indexes:** no frequent selective subset or index-only query
  was identified in the measured workload.
* **Partitioning the account table:** it cannot enforce a global unique fingerprint
  across partitions unless the partition key is in the unique index, which would
  change duplicate semantics. See section 6.

## 3. Query plans (880k accounts, `EXPLAIN (ANALYZE, BUFFERS)`)

| Query | Plan | Time | Buffers |
| --- | --- | ---: | ---: |
| Exact email, 100 newest accounts | indexed lookup (plan detail not captured) | 0.04 ms | 8 |
| Rare substring, URL ids | bitmap scan on the trigram GIN | 14.4 ms | 1,435 |
| Common substring, URL ids | seq scan (matches every row; correct choice) | 107.8 ms | 2,227 |
| `count(*)` over accounts | aggregate over the smallest index | 45.3 ms | 1,032 |
| Page of 100 at 50 % depth, `OFFSET` | scan and discard 440 k rows | 43.6 ms | 1,205 |
| Same page, keyset `id > cursor` | index range scan | 0.07-0.31 ms | 4 |

Queries produced by the views were not rewritten. The existing two-step page
load (page ids first, then `in_bulk` of the 100 rows) already avoids joining
five tables for every match. No N+1 pattern was found in the paths reviewed (search, browse, reference pages);
reference pages count usage only for the current page (existing).

**Remaining query costs:** exact `COUNT(*)` of a broad match and the sort of the
matched ids are linear in the match set (e.g. 84 ms for the domain that covers
half the data, 240-415 ms for broad substrings at 0.88 M accounts); they will grow
with data. Options, none implemented because each needs a product decision:
cached or "at least N" counts with a freshness label (contract R5), result-size
caps for substring search, or a read replica.

## 4. Pagination and counts

Contract (implemented and tested in `tests/tests_pagination.py`; README section
"Counts and pagination contract"):

* Default: exact counts and page numbers, unchanged.
* Opt-in cursor mode for id order: `?sort=id|-id&after=<id>`; no total, constant
  cost per page, stable under concurrent inserts (no repeats or skips behind the
  cursor), measured 140-600x cheaper at 50 % depth.
* No estimate is ever shown as exact.
* Search totals: exact by default. `VAULT_RESULT_COUNT_CAP=N` computes a total from
  at most N+1 rows, shows `N+` and a note when exceeded, and is exact for any search
  with at most N matches (`vault/counts.py`, `tests/tests_counts.py`). Measured
  45 ms versus 1,095 ms for a whole broad search at 8.27 M accounts.
* Whole-table totals (dashboard, Records, References): exact below 1 M estimated
  rows; above, an exact count cached for `VAULT_TOTAL_COUNT_TTL` (60 s) and refreshed
  by every UI add/delete. Dashboard 575 -> 34 ms at 7.4 M accounts.
* Not covered: the keyword search over five reference tables, whose cost is the
  trigram bitmap on a broad pattern (about 2 s idle at 8.3 M), not the count.

## 5. Batch writes and transactions

* Set-based `COPY` into temp tables, `ON CONFLICT DO NOTHING` upserts and one
  `INSERT ... SELECT` for accounts already existed; no per-row statements were
  found in the durable path.
* Durable batches use `synchronous_commit = on`, `work_mem` 64 MB (hard cap
  256 MiB per node), one transaction per batch; the stale statement in the
  README that the importer sets `off` was corrected, and the compatibility
  loader is the only code that uses `off`.
* Batch size: 250,000 rows (default) was 20 % faster than 10,000 and equal in
  WAL; larger batches raise importer memory (about 2.5 KB per buffered row) and
  the amount lost to recompute on a crash.
* Ledger: one row per source line is still written, with a hash instead of
  plaintext, and nothing was shortened or deleted. At 527 bytes per source line
  (after 0008) the ledger is 30 % of storage. Retention, compaction or
  partitioning of old ledger rows **requires an approved audit-retention policy
  (R6) and a migration plan**; because `ImportRecord` rows are referenced by
  recovery and job pages, partitioning by `import_job_id` or time is the
  natural design **[future, not implemented]**.
* Autovacuum: not changed. Bulk loads create index churn; `VACUUM (ANALYZE)`
  after a large load (the README already recommends it) was used between
  runs. Vacuum and checkpoint behaviour under sustained load were not
  characterized.

## 5b. Importer SQL changes from the second pass

* Every `INSERT ... SELECT ... ON CONFLICT` into a table with a unique key
  (five reference tables, the account table) now has `ORDER BY` that key, so
  concurrent transactions wait on one another in one direction only (deadlock
  fix, `docs/scalability/BENCHMARK_RESULTS.md` 22.1). Measured +3 % throughput at 1.13 M
  accounts (sorted index inserts); not measured at 8 M.
* `refresh_stale_reference_stats` analyzes a joined reference table inside the batch
  transaction only when its `reltuples` is 0 and it has at least 16 pages. A table
  analyzed while empty otherwise stays at "1 row" and the account INSERT degrades
  into a nested loop (22.2). Healthy tables are never analyzed per batch.
* `refresh_stale_account_stats` does the same for `vault_account` after the account INSERT,
  because the ledger's deferred foreign key to it is checked at COMMIT (section 25 of the
  results). Stale ledger statistics alone had no measurable effect.

## 6. Partitioning and sharding assessment

Prototype measured (`benchmarks/sql/partition_prototype.sql`, results in
`docs/scalability/BENCHMARK_RESULTS.md` section 19, 1 M rows per table, everything cached):

* Hash partitioning the **ledger** by `import_job_id` (16 partitions) with
  `UNIQUE (import_job_id, idempotency_key)`: insert +9 %, key-only lookup 2.8x
  slower (all partitions probed), same lookup with the job id 107 ms for 20,000
  probes (pruned), per-job aggregate +8 %, `REINDEX` +57 %, size +5 %. No gain on
  one node at this size.
* Hash partitioning the **account** table by `fingerprint` keeps
  `UNIQUE (fingerprint)` valid, but PostgreSQL then refuses `UNIQUE (id)` ("unique
  constraint on partitioned table must include all partitioning columns"), so the
  ledger's `account_id` foreign key cannot be kept; the composite primary key
  `(id, fingerprint)` also made the table 26 % larger. Not adopted.
* The product has no tenant or workspace key; none was invented. A sharded design
  would need global dedup by fingerprint routing, reference tables sharded by value
  hash and scatter-gather for substring search. Architectural estimate only.
* What partitioning is *for* (per-partition `VACUUM`, `REINDEX CONCURRENTLY`, dropping
  or archiving a job's ledger) could not show up at 1 M cached rows. Revisit with the
  triggers in `docs/scalability/PERFORMANCE_TARGETS.md` R8, and only together with a retention
  policy (R6) because the ledger is the audit trail.

## 7. Migration and rollback summary

| Migration | Effect | Safety |
| --- | --- | --- |
| `0008_ledger_index_cleanup` | drops 4 redundant indexes (see section 2) | 0.8 s forward on 1 M ledger rows; reverse recreates all indexes in 5.4 s (full table scans; seconds-minutes at scale); `DROP INDEX` takes a brief exclusive lock; take a backup before running on a large database; no data changes; unique constraints untouched |

| `0009_drop_reference_like_indexes` | drops the five reference `*_like` (varchar_pattern_ops) indexes and the two ledger `status` indexes (737 MB, 7 % at 7.4 M accounts; zero reads) | forward 0.4 s on 8.3 M accounts; reverse recreates seven indexes in 30.6 s (table scans); brief exclusive lock per `DROP INDEX`; take a backup first on a large database; no data change; unique constraints untouched; `ImportRecord.status` loses `db_index` (Django state updated, `makemigrations --check` clean) |

`makemigrations --check` reports no drift.
