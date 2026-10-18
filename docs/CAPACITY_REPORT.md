# Scalability and capacity report (deliverable G)

## 1. Bottom line

* **Verified:** correct, durable, recoverable ingestion and fast exact/substring
  search up to **1.06 M accounts-in-flight (0.88 M stored + 0.18 M imported)** on a
  2-vCPU, 3.9 GB sandbox, including searches running during the import. This is
  a small single machine; absolute numbers say little about production hardware.
* **Not verified:** anything at or above 10 M accounts, any billion-record
  behaviour, multi-core or multi-disk scaling, PostgreSQL crash/restart, power
  loss, failover, backup restore, vacuum/checkpoint behaviour under sustained
  load, cold-cache reads.
* **Do not read this as billion-record support.** The repository, even after
  these changes, is a single-primary PostgreSQL design. Section 5 lists what must
  be demonstrated before such a claim can be made.

## 2. Directly measured (this environment)

| Measure | Value |
| --- | --- |
| Largest database tested | 879,519 accounts + 1,000,000 ledger rows (+176 k during experiments) |
| Durable import into an empty database | 4,830 lines/s (1 M lines) |
| Incremental import, 880k existing, no queries | 4,331-4,548 accounts/s |
| Same with two concurrent query workers | 3,342-3,431 accounts/s |
| Storage | 1,970 B per account including the ledger (1,497 without); 527 B per ledger line |
| WAL | about 6.2 KB per imported line (1.09 GB / 176 k accounts, 200 k lines) |
| Importer memory | 637 MB single process (735 MB tree) at 1 M lines |
| Exact lookup latency during import | p50 about 11 ms, p95 42-56 ms (idle p50 10 ms) |
| Broad substring / domain during import | p50 210-370 ms, p95 300-690 ms (idle 80-150 ms) |
| Recovery | SIGKILL mid-import; 118,418 remaining lines replayed in 23.3 s; counts identical to an uninterrupted run |

## 3. Measured at smaller scale and projected (explicit assumptions)

These are arithmetic on the measurements above, **not forecasts**:

| Quantity | Projection at 1 B unique accounts | Assumptions that probably fail |
| --- | --- | --- |
| Table + index storage | about 1.97 TB (1,970 B x 1 B) | constant bytes per row; index depth and value cardinality grow; reference tables may deduplicate more or less than in the synthetic mix |
| Ledger share | about 0.5 TB of that, plus 1 row per source line including duplicates and invalid lines | duplicate rate 10 %; plaintext not stored |
| Import time at the measured 4,300 accounts/s | about 64 hours | throughput was already falling with size in the repository's earlier 0.5 M -> 2.75 M measurements (6.6 k -> 3.6 k/s) |
| WAL volume | about 6 TB written over the whole load at 6.2 KB per line | `wal_compression` (-32 % measured), batch layout and checkpoint settings change this a lot; first-touch page images dominate in the sandbox, and it must be archived or recycled fast enough |
| `COUNT(*)` of all accounts | about 55 s (50 ms x 1,136) | linear extrapolation of a warm index-only count; cold would be worse |

These show why a linear extrapolation is not a plan: the import time, WAL volume
and `COUNT(*)` rows are each large enough that WAL settings, checkpoint tuning,
batch layout and the count contract would have to be revisited before such a load.

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
  idempotency key). See `docs/DATABASE_OPTIMIZATION_REPORT.md` section 6.

## 5. Progressive validation plan (proposed, none beyond row 1 executed)

For every stage record: total and unique records, file count and size mix,
duplicate and invalid rates, ingest rate by stage (read, parse, DB write),
table/index/ledger/WAL sizes, p50/p95/p99 for the `perfbench concurrent`
query set during an import, CPU/memory/IO/connection use, vacuum and
checkpoint logs, recovery time after SIGKILL and after a PostgreSQL restart,
retry/failure counts, and cost. Gate each stage on the approved targets
(`docs/PERFORMANCE_TARGETS.md`) before starting the next.

| Stage | Accounts | Status |
| --- | ---: | --- |
| 0 | 0.88 M (1 M lines) | **done** (this report) |
| 1 | 10 M | not run; first stage to run on production-like hardware |
| 2 | 50 M | not run |
| 3 | 100 M | not run; decide on ledger partitioning and cached counts here |
| 4 | 250 M | not run |
| 5 | 500 M | not run; decide on sharding vs. larger primary |
| 6 | 1 B | not run; claim only if stage 5 SLOs hold with margin |

Data generation: `perfbench generate` is deterministic and fast enough
(1 M lines in about 2 s) but its host skew and 10 % duplicates are assumptions;
use a sample of real distributions if available.

## 6. Next architectural thresholds (what will break first, by evidence)

1. **Exact counts and broad matches** become the slowest interactive operations
   (linear in the match set).
2. **WAL and index maintenance** dominate import cost (6.2 KB WAL per line,
   index page writes); batch layout and `wal_compression` are the first levers.
3. **Ledger growth** (30 % of bytes) needs a retention decision (R6) and probably
   partitioning.
4. **RAM for indexes** decides whether lookups stay at tens of milliseconds.
5. Anything above one primary's disk/RAM/WAL capacity needs the sharding design
   that cannot be chosen without a shard key (R8).
