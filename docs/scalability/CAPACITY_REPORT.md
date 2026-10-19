# Scalability and capacity report (deliverable G)

## 1. Bottom line

* **Verified (single 2-vCPU / 3.9 GB sandbox, PostgreSQL 16.2):** correct, durable,
  recoverable ingestion and fast exact search up to **8.27 M accounts / 9.4 M ledger
  rows (11.5 GB)** in one database, with searches running during imports. Exact
  lookups stay about 9 ms idle and p95 20-36 ms during imports at every size
  tested. Server crash, importer kill and point-in-time recovery drills lose or
  duplicate nothing.
* **What bends first is ingest, not search:** durable ingest fell from 6,049
  accounts/s (empty) to about 1,800 accounts/s at 8 M accounts on this host, and
  to 870-1,750 accounts/s when two query workers run (`docs/scalability/BENCHMARK_RESULTS.md`
  sections 12-14). Broad searches and counts grow linearly with the match set and
  are now bounded by an opt-in cap and cached totals.
* **Not verified:** 10 M accounts and above (this sandbox's 21 GB disk stops the
  ladder near 8-9 M), any billion-record behaviour, multi-core or multi-disk
  scaling, power loss with a disk that loses acknowledged writes, failover and
  replicas, cold-cache reads, vacuum behaviour over days, restore time at terabytes.
* **Do not read this as billion-record support.** The code remains a
  single-primary PostgreSQL design. The partitioning prototype (section 19 of the
  results) showed no single-node gain, and sharding needs a shard key that the
  product does not have.

## 2. Directly measured (this environment)

| Measure | Value |
| --- | --- |
| Largest database tested | 8,271,888 accounts, 9,399,671 ledger rows, 11.53 GB, 0 duplicate fingerprints |
| Durable import into an empty database | 6,049 accounts/s (1 M lines in 145 s) |
| Durable import, 2.6 M -> 5.5 M accounts (3 M lines) | 3,105 accounts/s |
| Durable import at 5.6-8.1 M accounts, no queries | 1,714-2,095 accounts/s |
| Durable import with two query workers | 4,435 (0.88 M), 1,753 (2.6 M), 1,019 (5.5 M), 870 (7.4 M) accounts/s |
| Storage per account, all tables and ledger | 1,968 B (0.88 M) falling to 1,495 B (8.27 M) with this generator's value repetition; 3,425 MB accounts + 4,594 MB ledger at 8.27 M |
| WAL per imported account | 6.0 KB (empty) to 17-21 KB (176 k-account imports at 8 M); 84 % full-page images |
| Importer memory | 637 MB single process (735 MB tree) at 1 M lines |
| Exact lookup latency during imports | p50 11-12 ms, p95 20-36 ms at 0.88 M to 7.4 M accounts |
| Broad search idle, exact total | 1,093 ms (common URL substring), 2,154 ms (keyword), `count(*)` 231 ms at 8.27 M |
| Broad search with `VAULT_RESULT_COUNT_CAP=100000` | 45 ms (whole view) at 8.27 M |
| Keyset page at 50 % depth versus `OFFSET` | 0.1 ms versus 490 ms at 8.27 M |
| Whole-table totals with the cached total | dashboard 34 ms (was 575), Records 3.5 ms (was 189) at 7.4 M |
| Recovery after SIGKILL of the importer | 118,418 lines replayed in 23.3 s; counts identical |
| Recovery after `pg_ctl stop -m immediate` | server redo 5.6-13.4 s, remaining 651,890 / 825,881 lines replayed in 91-120 s; counts identical; `pg_amcheck` clean |
| Point-in-time recovery | backup 5.5 s, restore plus WAL replay 15.1 s at 1.8 GB; counts exact at the target time |

## 3. Measured at smaller scale and projected (explicit assumptions)

These are arithmetic on the measurements above, **not forecasts**:

| Quantity | Projection at 1 B unique accounts | Assumptions that probably fail |
| --- | --- | --- |
| Table + index storage | 1.5-2.0 TB (1,495-1,968 B per account) | constant bytes per row; the synthetic generator's value repetition set the range; real data repeats differently; index depth grows |
| Ledger share | 0.55 TB (4.6 GB / 8.27 M) plus one row per source line | duplicate rate 10 %; plaintext not stored |
| Import time | at least 6.4 days even if the rate stayed at the 8 M value of 1,800 accounts/s | the measured rate was **falling** with size, so this is a lower bound; at 4,300/s (the earlier 0.88 M figure) it would be 64 h, which the data above contradict |
| WAL volume | 6-20 TB over the load (6-21 KB per account measured) | full-page images dominate and depend on checkpoint spacing and index size; `wal_compression=pglz` halved the rate here |
| Exact `COUNT(*)` of all accounts | 28 s (ORM, 231 ms x 121) to 66 s (SQL index-only, 548 ms x 121) | linear extrapolation of a warm count; cold would be worse |
| Exact total of a broad search | about 130 s (1.09 s x 121) | linear in matches (measured linear from 0.88 M to 8.27 M) |
| Same search with the cap | about 45 ms plus plan risk | the bounded count is independent of the match count; one data distribution tested |
| Exact lookups | tens of milliseconds while the index working set is cached | measured flat to 8.27 M with a 3.9 GB host holding 11.5 GB of data; cold-cache latency at 1 B was not measured |

These show why a linear extrapolation is not a plan: import time, WAL volume and
the full-table count are each large enough that WAL settings, checkpoint tuning,
batch layout, bigger RAM and a partitioning or archive policy would have to be
revisited before such a load.

## 4. Architectural estimates (not tested)

* One primary with enough RAM to keep the five reference B-tree/trigram indexes
  and the account FK indexes hot is the cheapest option. At 1,970 B per account
  the hot index set alone is roughly 60-70 % of the data size; for 1 B accounts
  that is more than 1 TB of RAM-resident index for fully cached lookups, which
  one commodity host does not provide. Indexes that do not fit in memory turn each
  probe into several random page reads (B-tree depth; cold-cache latency was not measured).
* Read replicas would offload search but add lag and do not increase write
  capacity; every replica is another copy of credentials (same access controls
  required).
* Horizontal scaling needs a deliberate shard key. The product has no tenant or
  workspace key, and uniqueness is global (fingerprint, reference names,
  idempotency key). See `docs/scalability/DATABASE_OPTIMIZATION_REPORT.md` section 6.

## 5. Progressive validation ladder

Gate each stage on the targets in `docs/scalability/PERFORMANCE_TARGETS.md`. For every stage
record: total and unique records, file count and size mix, duplicate and invalid
rates, ingest rate by stage, table/index/ledger/WAL sizes, p50/p95/p99 for the
`perfbench concurrent` query set during an import, CPU/memory/IO, vacuum and
checkpoint logs, recovery time after a kill and after a PostgreSQL crash, retry
and failure counts. `benchmarks/scale_ladder.sh` does this per stage.

| Stage | Accounts | Status |
| --- | ---: | --- |
| 0 | 0.88 M (1 M lines) | **done** (145 s import, queries, storage) |
| 1 | 2.64 M | **done** (396 s; concurrent run) |
| 2 | 5.46 M | **done** (850 s; concurrent run) |
| 2b | 7.4 M and 8.27 M | **done** (experiments, capped concurrent run, query/storage/count suites) |
| 3 | 10 M | **not run**: needs more disk than this sandbox (about 14 GB of tables plus 4 GB WAL); first stage for production-like hardware |
| 4 | 50 M | not run |
| 5 | 100 M | not run; the partitioning/retention decision belongs here if ingest has not recovered |
| 6 | 250 M | not run |
| 7 | 500 M | not run; decide sharding versus a larger primary |
| 8 | 1 B | not run; claim only if stage 7 holds with margin |

The next run on production-like hardware should first repeat stages 0-2 (to
calibrate against this report), then continue. Data generation: `perfbench
generate` is deterministic and fast (1 M lines in about 2 s); its host skew, 10 %
duplicates and value repetition are assumptions, so use a sample of real
distributions if one can be provided safely.

## 6. Next architectural thresholds (what will break first, by evidence)

1. **Ingest rate at size** (3x loss between 0.9 M and 8 M on this host): random-key
   unique indexes beyond RAM and full-page WAL. First levers: RAM and
   `shared_buffers` sized to the hot index set (note: 2 GB of 3.9 GB made it worse
   here), `max_wal_size` above 4 GB (untested, disk limit), `wal_compression=lz4` on a
   multi-core host (not available in this build; `pglz` halved the rate on 2 vCPU).
2. **Exact counts and broad matches** (linear): addressed by the opt-in cap and the
   cached totals; keyword search through five trigram bitmaps is **not** addressed
   (about 2 s idle at 8.3 M).
3. **Ledger growth** (40 % of table bytes): needs an archive-first retention design
   (R6); partitioning it showed no single-node gain.
4. **RAM for indexes** decides whether lookups stay at tens of milliseconds.
5. Anything above one primary's disk/RAM/WAL capacity needs the sharding design
   that cannot be chosen without a shard key (R8).
