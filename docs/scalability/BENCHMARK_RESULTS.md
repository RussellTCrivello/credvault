# Before / after results (deliverables E and F)

All numbers were measured on 2026-10-18 in one sandbox: 2 vCPU, 3.9 GB RAM, one
virtual disk, PostgreSQL 16.2 (Unix socket, `shared_buffers=512MB`,
`fsync=on`, `synchronous_commit=on`, `wal_compression=off`), Python 3.11.2,
Django 5.2.18, synthetic data from `perfbench generate`. Reproduction steps:
`docs/scalability/BENCHMARK_SUITE.md`. Raw JSON/console output: `benchmarks/results/2026-10-18/` (sections 0-11) and
`benchmarks/results/2026-10-19/` (sections 12-20, taken on a rebuilt sandbox of the
same size; the 0.88 M numbers were re-measured, so absolute values differ from
sections 0-11 by up to 30 % because the VM and PostgreSQL build were recreated).

**How to read this.** Each row compares variants inside one run set on the
same cloned database. Most are single runs on a shared VM; where a variant was
repeated, both runs are shown and the spread was 1-3 %. Nothing below is a
production or billion-record claim (see `docs/scalability/CAPACITY_REPORT.md`).

## 0. Test data

| Data set | Content | Used for |
| --- | --- | --- |
| Base | 1,000,000 lines / 8 files (seed 7, 10 % duplicate lines, 2 % malformed) imported durably into an empty database: **879,519 accounts**, 120,481 skipped, 0 failed; 195,960 URLs, 753,893 emails, 125,626 usernames, 879,519 passwords, 576,601 IPs; 1,000,000 ledger rows | populated-database experiments |
| Increment | 200,000 lines (seed 21, 2 files; or seed 33, 4 files), 10 % duplicates, 2 % malformed: **176,007 / 176,179 new accounts** | incremental imports |
| Skew | host index is Pareto-distributed: about half of all records share `host1.example.com` | broad "domain" and cursor queries |

## 1. Where the time goes before the database (stage profile)

`perfbench stages`, 200,000 lines, 17.6 MB, warm page cache, one core:

| Stage | Wall | Share of in-process pre-DB work |
| --- | ---: | ---: |
| Raw file read (64 KiB chunks) | 0.003 s (5.7 GB/s) | not counted (cache hit) |
| Bounded line split | 0.066 s (3.0 M lines/s) | 2.4 % |
| Per-line SHA-256 (audit hash) | 0.126 s | 4.6 % |
| UTF-8 decode | 0.031 s | 1.1 % |
| Parse + validate + normalize | **2.49 s** (80 k lines/s, 12.5 CPU-us/line) | **91.8 %** |

Chunk size matters only for the raw read stage: 4 KiB reads took 0.040 s versus
0.003 s at 64 KiB and 1 MiB, still about 1.5 % of the parse stage; line split,
hash and decode were unchanged and the parse-stage differences between runs
(2.43-2.82 s) are noise. **Conclusion (measured):** file
reading and segmentation are not a bottleneck on this host with a warm cache;
parsing is. Splitting one file into byte-range segments would only parallelize
a stage that costs 2-3 % of the work; parser-process parallelism (already
present) is what matters. Cold-cache and fast-storage behaviour were not measured.

## 2. Parse optimization: bounded host-normalization cache

Bottleneck: `normalize_host` was 23 % of `parse_line` under cProfile and hosts
repeat heavily inside a file. Change: a 4,096-entry per-process LRU on that pure
function (host names only; never passwords).

| Case | Before | After | Change |
| --- | ---: | ---: | ---: |
| 200k skewed lines, parse stage | 3.33 s (60.1 k lines/s) | 2.49 s (80.2 k lines/s) | **-25 % time** |
| 100k lines, every host unique (worst case) | 1.31 s | 1.21 s | no regression measurable (noise) |

Correctness: `HostCacheTests` compares cached and uncached output for 17 hosts
(IDN, IPv6 literals, trailing dots, overlong, invalid, bytes input).

## 3. Ledger and account index clean-up (migration 0008)

Bottleneck: `perfbench storage` showed three byte-identical 124.7 MB indexes on
`vault_importrecord.idempotency_key` (`_key` unique, `vault_importrec_idem_idx`,
`..._like`), a 110 MB `varchar_pattern_ops` index on the fingerprint and a 6 MB
FK index that is a prefix of two composite indexes. Code review found only
equality / `IN` lookups on these columns.

| Metric (880k accounts + 1M ledger rows) | Before | After | Change |
| --- | ---: | ---: | ---: |
| Vault tables, total | 2,098.9 MB | 1,732.8 MB | **-17.4 %** |
| Bytes per account (including ledger) | 2,386 | 1,970 | -17.4 % |
| Ledger bytes per source line | 783 | 527 | -32.7 % |
| 200k-line online import, 2 query workers (2 runs each) | 68.6 s / 69.9 s | 51.3 s / 52.7 s | **-25 %** |
| Accounts per second | 2,564 / 2,516 | 3,431 / 3,342 | **+33 %** |
| WAL written | 1,531 / 1,454 MB | 1,089 / 1,091 MB | -26 % |
| Database growth | 346 / 346 MB | 274 / 274 MB | -21 % |

Migration safety (on the 1M-line ledger): forward 0.8 s, reverse 5.4 s, reverse
restores all 19 indexes. Unique constraints are untouched (`LedgerIndexTests`).

## 4. Fast mode versus online ingestion (searches during import)

200k-line durable import into the 880k-account database, two query workers
looping over seven real view queries with 50 ms think time. "During" = samples
taken while the import ran; "idle" = 10 samples before it started.

| Variant | Import (incl. index rebuild) | Accounts/s | WAL | Query errors |
| --- | ---: | ---: | ---: | --- |
| A. Online, old schema (run 1 / run 2) | 68.6 s / 69.9 s | 2,564 / 2,516 | 1.53 / 1.45 GB | none |
| B. Online, new schema (run 1 / run 2) | 51.3 s / 52.7 s | 3,431 / 3,342 | 1.09 GB | none |
| C. `--fast`, old behaviour (31 btree + trigram indexes suspended) | 77.2 s | 2,280 | 1.14 GB | one `OperationalError` (message not captured) |
| D. `--fast`, new default (only the 5 trigram indexes suspended) | 102.7 s | 1,714 | 0.79 GB | none |

Latency (ms; idle p50 -> during p50 / p95 / p99 [n = samples during import]):

| Query | Idle p50 (B) | B online, new schema | D fast, trigram only | C fast, old behaviour |
| --- | ---: | --- | --- | --- |
| Exact email | 10.3 | 10.7 / 41.7 / 71.0 [74] | 10.7 / 42.2 / 77.4 [48] | **652.9 / 782.1** [7] |
| Exact domain, rare host | 19.7 | 27.4 / 64.6 / 74.7 | 159.6 / 203.3 / 252.1 | 3,724.8 / 4,711.9 [6] |
| Exact domain, broad (about half of all rows) | 80.1 | 211.6 / 306.3 / 323.3 | 226.4 / 298.9 / 335.5 | 4,719.2 / 7,034.4 [7] |
| Substring URL, rare | 25.0 | 34.2 / 66.5 / 93.0 | 151.9 / 211.5 / 375.9 | 98.1 / 192.0 [6] |
| Substring URL, broad | 106.5 | 262.0 / 406.6 / 459.4 | 1,324.6 / 1,993.5 / 2,449.4 | 727.8 / 1,163.7 [6] |
| Substring keyword | 120.2 | 368.1 / 574.8 / 935.6 | 1,825.6 / 2,483.1 / 2,541.6 | 1,353.6 / 2,073.5 [6] |
| Cursor page, broad | 39.1 | 75.7 / 203.2 / 258.6 | 70.0 / 166.9 / 247.2 | 44.2 / 59.0 [6] |

Interpretation, limited to this regime:

* With every read index maintained (B) the import is the fastest variant and
  search stays available: exact lookups keep a p50 of about 11 ms and p95 of
  about 42-56 ms; broad queries roughly double (CPU is shared by two query
  workers, two parse workers and PostgreSQL on 2 vCPUs).
* Suspending indexes **did not make this import faster** here (C and D are
  slower than B). The rebuild at the end is included in their time and is the
  likely cause (rebuilding over a 1.06 M-row table versus 176k incremental
  inserts), but the rebuild time was not isolated. It also degraded reads by
  5-70x. The earlier
  README gain (12-22 %) was measured on a load into an empty database; that
  regime was not re-measured.
* Decision: `--fast` now suspends only the five trigram indexes (what the
  documentation always promised); `VAULT_FAST_MODE_SCOPE=all` keeps the broad
  behaviour for offline loads. The default online mode remains the default.
* Sample counts for C and D are small (the loop is blocked by index builds);
  treat their percentiles as indicative.

## 5. Batch size (no concurrent queries)

Same 200k-line import into the 880k-account database, new schema,
`VAULT_IMPORT_BATCH_ROWS` (the files hold 100k lines each, so 250,000 means one
batch per file):

| Batch rows | Time | Accounts/s | WAL | Peak RSS |
| ---: | ---: | ---: | ---: | --- |
| 10,000 | 48.7 s | 3,616 | 1,082 MB | not measured |
| 50,000 | 46.4 s | 3,791 | 1,092 MB | not measured |
| 250,000 (default) | 40.6 s | 4,331 | 1,092 MB | 559 MB (see section 8) |
| 250,000 + `wal_compression=pglz` | 45.1 s | 3,900 | 740 MB (-32 %) | n/a |

Larger batches amortize per-batch SQL planning and ANALYZE; the default stays at
250,000. Batch size did not change WAL volume (dominated by index page writes).
`wal_compression` cut WAL by a third at an 11 % time cost on a CPU-starved box;
it is a deployment decision, not changed by the code. Transaction duration,
lock contention and rollback cost per batch size were not isolated.

## 6. File-level parallelism (`importfolder --parallel-files`)

Two different regimes gave opposite answers, so the option stays opt-in.

**(a) Populated database, DB-write bound.** 200k lines in 4 files into the
880k-account clone, batch 250k, no queries:

| `--parallel-files` | Parse workers per file | Time | Accounts/s | New accounts | Duplicate fingerprints |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 (default) | 2 | 38.7 s | 4,548 | 176,179 | 0 |
| 2 | 1 | 40.1 s | 4,388 | 176,179 | 0 |
| 4 | 1 | 43.7 s | 4,029 | 176,179 | 0 |

No gain (-3.5 % and -11 %) and WAL +9 % at four files: the PostgreSQL backend
already saturates a core on the larger indexes. Results are identical, so
concurrent jobs preserve global uniqueness (including cross-file duplicates).

**(b) Empty database, parse bound** (`FileConcurrencyBenchmarkTests`, 100,000
unique records, one run each; `--parallel-files=1` reads one 100k-line file with
2 parse workers, 2 and 4 use 4 files of 25k):

| `--parallel-files` | Time | Records/s |
| ---: | ---: | ---: |
| 1 | 18.1 s | 5,513 |
| 2 | 14.0 s | 7,134 (+29 %) |
| 4 | 14.1 s | 7,072 (+28 %) |

A 20,000-line run of the same test showed 5,661 / 7,569 / 6,912 records/s.
Interpretation (plausible, not isolated): when indexes are small, parsing is the
limit and overlapping two jobs' parse and write phases helps; when the database
is large and the write path dominates, extra jobs only add contention. Both are
single runs on 2 vCPUs. Benefit on more cores or high-latency storage is
**unverified**. Segment-level (intra-file) parallel reading was not implemented
because the stage profile (section 1) shows read/split is 2-3 % of the
pre-DB work, and the per-line positions in the ledger would need a pre-pass to
assign gap-free numbers per segment.

## 7. Durability

The durable batch sets `SET LOCAL synchronous_commit = on` in every flush path
(code lines in `vault/import_engine.py`; test asserts no `off`). Only the
compatibility loader in `vault/importer.py` uses `off`. Cost probe, 300 single-row
transactions: `on` p50 0.53 ms / p95 0.62 ms versus `off` 0.41 / 0.58 ms. The
durable path commits once per batch (10 batches for 100k rows at 10k), so the
measured per-commit difference is below 0.1 % of batch time here. Caveat: the
sandbox disk acknowledges flushes quickly; network storage will cost more and
should be re-measured.

## 8. Memory (bounded-memory check) and the reference-cache bound

Peak resident memory of the whole process tree (parent + parse workers), default
batch 250,000, populated clone. "Before" = per-job reference cache unbounded
(it retained every distinct URL/email/user/password/IP of the job); "after" =
cache cleared at 200,000 values per kind and on any rolled-back batch
(`MAX_REFERENCE_CACHE_VALUES`).

| Input | Cache | Time | Peak single process | Peak process tree |
| --- | --- | ---: | ---: | ---: |
| 200k lines (one 17.6 MB file) | unbounded | 38.8 s | 559 MB | 661 MB |
| 200k lines | bounded | 40.1 s | 556 MB | 658 MB |
| 1M lines (one 89 MB file) | unbounded | 207.0 s | 789 MB | 887 MB |
| 1M lines | **bounded** | 201.0 s | **637 MB** | **735 MB** |

At 1 M lines the bound cut peak memory by 19 % (-152 MB) at no throughput cost;
with the unbounded cache, memory grew with the count of distinct values
(559 -> 789 MB for 5x the input), which is not bounded for a 100 M-line file. With
the bound, the remaining footprint is the 250,000-row batch plus bounded parse
queues (about 2.5 KB per buffered row); reduce `VAULT_IMPORT_BATCH_ROWS` to lower
it (trade-off in section 5). The bounded run was measured at 1 M lines only;
that memory stays flat beyond that is expected from the limits but **not
measured**. PostgreSQL's own memory (`shared_buffers`, `work_mem` up to
256 MiB per node) is extra. In-process limits: 16 KiB per source line (longer
lines are hash-only audited), 8 MiB per parse task, 64 MiB queued parse input
and 256 MiB buffered rows/metadata per commit.

## 9. Recovery (SIGKILL mid-import)

200k lines, batch 20,000, `kill -9` of the import process tree after about 22 s;
then `manage.py recoverimports --run`:

| Quantity | Value |
| --- | ---: |
| Lines durable at kill (batch-granular) | 81,582 (71,996 accounts + 9,586 skipped) |
| Time to recover and replay the remaining 118,418 lines | 23.3 s |
| New accounts after recovery | 176,179 (equals the uninterrupted run) |
| Ledger rows added / distinct idempotency keys | 200,000 / 200,000 |
| Duplicate fingerprints | 0 |

Process-level crash only. Not tested: PostgreSQL crash/restart, host or power
loss, disk full, backup restore, replica failover.

## 10. Query paths and pagination (880k accounts, idle)

`perfbench queries`, 7 runs, real views (p50 / p95 ms):

| Query | p50 | p95 |
| --- | ---: | ---: |
| Exact email | 10.1 | 11.1 |
| Exact domain (about half of all rows, exact count) | 83.6 | 94.1 |
| Substring URL, rare | 21.4 | 22.7 |
| Substring URL, common (matches every URL) | 204.3 | 285.8 |
| Substring keyword, common | 384.1 | 415.8 |
| Deep page (200) of a common substring | 237.5 | 252.3 |
| `COUNT(*)` over accounts | 23.6 | 24.3 |

`EXPLAIN (ANALYZE, BUFFERS)`:

| Statement | Execution | Shared buffers |
| --- | ---: | ---: |
| `count(*)` of 880k accounts | 45.3 ms (index-only aggregate) | 1,032 hit |
| Rare substring URL ids (`upper(name) LIKE`, trigram) | 14.4 ms, Bitmap Heap Scan | 1,435 hit |
| Common substring URL ids | 107.8 ms, Seq Scan (matches everything, planner is right) | 2,227 hit |
| Exact email -> 100 newest accounts | 0.04 ms | 8 hit |
| Page of 100 ids by `OFFSET` at 50 % | 43.6 ms | 1,205 hit |
| Same page by keyset (`id > cursor`) | 0.07-0.31 ms | 3 hit + 1 read |

Keyset removes the offset penalty (about 140-600x here) but only for id order;
exact `COUNT(*)` and broad substring matches still scale with the matching set.

## 11. Import throughput, populated versus empty

| Run | Throughput | Notes |
| --- | ---: | --- |
| 1M lines into an empty database, durable, defaults | 4,830 lines/s (207 s) | 879,519 new |
| 200k lines into 880k accounts, online, new schema, no queries | 4,331-4,548 accounts/s | 176k new, batch 250k |
| Same with two concurrent query workers | 3,342-3,431 accounts/s | -20-25 % |
| Pre-existing repository figure (500k new into 2.75M existing, different data mix and machine) | 3,600/s | not comparable |

The pre-change 100k-line tests earlier in this program measured durable 16.8 s
(5,958/s), compatibility bulk 12.2 s (8,185/s), compatibility fast mode 9.1 s
(11,006/s) on an empty database; those use a different synthetic generator
(`tests_performance.synthetic_line`) and are not comparable with the above.
The compatibility loader is not durable (`synchronous_commit=off`, no ledger).

## 12. Progressive-scale ladder (0.88 M to 8.27 M accounts, one database)

Raw data: `benchmarks/results/2026-10-19/` (`s0..s4_*.json`), script
`benchmarks/scale_ladder.sh`. Data: `perfbench generate`, 10 % duplicate lines,
2 % invalid lines, distinct seeds per stage. One database loaded cumulatively
with durable online imports (default settings, 2 parse workers, batch 250 k), then
`VACUUM (ANALYZE)`, then the query suite. Schema as in this branch (0008 applied;
0009 applied from stage 3 on). Single runs.

| Stage | Lines loaded | Accounts after | Import time | New accounts/s | Lines/s | WAL | Database |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 1.0 M into empty | 879,519 | 145 s | 6,049 | 6,877 | 5.25 GB (6.0 KB/account) | 1.62 GB |
| 1 | 2.0 M | 2,639,571 | 396 s | 4,439 | 5,044 | 11.67 GB (6.6 KB) | 4.45 GB |
| 2 | 3.0 M | 5,455,792 | 850 s | 3,105 | 3,529 | 23.46 GB (8.9 KB) | 8.78 GB |
| 3-4 | experiments of sections 13-14 and three concurrent runs (about 4.5 M further lines) | 8,271,888 | n/a | 1,714-2,095 (single 200-500 k-line imports at 5.6-8.1 M) | n/a | n/a | 11.53 GB (ledger 9.40 M rows) |

**The ingest rate falls steadily with size** on this host: 6,049 -> 4,439 -> 3,105
-> about 1,800 accounts/s while the data grew 9x (roughly with the square root of
size). The importer is database-bound (`db_busy_ratio` 0.88-0.91 in the runs of
section 20, which were taken at 0 and 0.88 M accounts; it was not recorded at
larger sizes). The most likely cause, supported by the WAL analysis in section
14 but **not isolated by a controlled experiment**, is that the random-key unique
indexes (`fingerprint`, reference `name`, ledger `idempotency_key`) outgrow RAM, so
most inserts touch cold pages and emit full-page WAL images. This is the most
important scale fact found: **the ingest curve, not the search curve, is what bends
first**; on this 3.9 GB host it fell to 30 % of the empty-database rate by 8 M
accounts. The ladder was not run beyond 8.3 M (21 GB disk).

Query latency, idle, p50 in ms through the real views (7 runs):

| Query | 0.88 M | 2.64 M | 5.46 M | 7.39 M | 8.27 M | Scales as |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| exact email | 8.2 | 9.1 | 8.8 | 9.0 | 9.0 | **flat** |
| exact domain (prefix range) | 67.9 | 80.1 | 93.0 | 103.8 | 111.3 | slowly with matches |
| substring URL, rare | 18.9 | 27.5 | 40.0 | 35.6 | 46.5 | slowly |
| substring URL, common (matches ~all rows) | 129.6 | 328.2 | 583.0 | 958.7 | 1,093 | linear in matches |
| substring keyword, common | 270.9 | 714.8 | 1,534 | 1,898 | 2,154 | linear in matches |
| deep page 200 of a common substring | 176.3 | 367.1 | 569.6 | 1,059 | 1,099 | linear (the count) |
| `count(*)` of all accounts (ORM) | 20.8 | 57.9 | 163.3 | 202.7 | 231.0 | linear |
| `OFFSET` page at 50 % (SQL, ms) | 44.2 | 145.0 | 301.4 | 415.6 | 490.0 | linear |
| keyset page at 50 % (SQL, ms) | 0.4 | 0.1 | 0.1 | 0.0 | 0.1 | **flat** |

Storage per account (all tables, indexes and ledger): 1,968 B at 0.88 M, 1,806 B at
2.64 M, 1,726 B at 5.46 M, 1,495 B at 8.27 M. The decrease is a property of the
synthetic generator (reference values repeat more at larger sizes), **not**
a storage saving to extrapolate. At 8.27 M: ledger 4.59 GB (1.90 GB of it indexes),
accounts 3.43 GB, passwords 1.91 GB, emails 1.19 GB.

Searches during an import (`perfbench concurrent`, 2 query workers, 50 ms think
time, a 175 k-account import of 200 k lines):

| Accounts before | Import accounts/s | exact email p50/p95 | broad domain p50/p95/p99 | broad URL substring p50/p95 | keyword p50/p95 |
| ---: | ---: | --- | --- | --- | --- |
| 0.88 M (online) | 4,435 | 10.8 / 36.7 | 194.8 / 295.4 / n.a. | 183.2 / 322.7 | 265.4 / 466.4 |
| 2.64 M | 1,753 | 11.4 / 35.9 | 248.7 / 363.0 / 463.5 | 267.6 / 391.6 | 334.0 / 565.4 |
| 5.46 M | 1,019 | 12.3 / 35.0 | 360.6 / 533.5 / 660.7 | 394.3 / 509.5 | 1,339 / 1,694 |
| 7.39 M, `VAULT_RESULT_COUNT_CAP=100000` | 870 | 11.7 / 20.1 | 99.4 / 146.9 / 287.5 | 107.5 / 193.6 | 831.5 / 1,281 |

Exact lookups stay interactive at every size. The query load roughly halves the
import rate (compare 1,714-1,835 accounts/s without queries at 7.6-8.1 M). Zero query
errors in all runs. The 7.39 M row used the cap and a larger table than the 5.46 M
row, which is conservative against the cap.

## 13. Index usage audit at 5.5 M accounts and migration 0009

`pg_stat_user_indexes` after loading 5.5 M accounts and running the suite:

| Index | Size | Scans | Decision |
| --- | ---: | ---: | --- |
| `vault_pass/email/ip/user/url_name_*_like` (varchar_pattern_ops) | 286/238/74/16/37 MB | 25,359 (pass, equality probes the planner can serve from `name_key`), 0, 0, 0, 0 | **dropped (0009)** |
| `vault_importrecord_status_*` and `_status_*_like` | 43 MB each | 0 | **dropped (0009)**; every status query is per job and uses `(import_job, status)` |
| `vault_*_name_lower_idx` | 286/238/74/16/37 MB | 0 (pass/ip/user, no exact search ran), 639 (email), 3,666 (url) | **kept**: `_match_exact_ids` filters on `lower(name)`; without them exact search is a sequential scan |
| `vault_importrecord_account_id_*` | 163 MB | 0 | **kept**: `ON DELETE SET NULL` needs it when an account is deleted |
| `vault_importrecord_idempotency_key_key`, `vault_account_fingerprint_*_uniq` | 760 / 670 MB | 0 / 6.1 M | unique constraints, untouchable |

Effect (three runs; every row a 440 k-account import of four 125 k-line files on
the same growing database, no queries):

| Run | Setting | Database before | Accounts/s | WAL |
| --- | --- | ---: | ---: | ---: |
| e1 | baseline, `shared_buffers=512MB`, `_like` indexes present | 5.63 M | 2,095 | 5.71 GB |
| e3 | the same again | 6.51 M | 1,934 | 6.73 GB |
| e4 | `_like` indexes dropped | 6.95 M | 2,005 | 6.52 GB |

Dropping the five reference `_like` indexes: **+3.7 % rate versus the nearest
baseline (within the 8 % run-to-run spread seen here), -3 % WAL, -651 MB (6 %
of the database) at 7.4 M accounts**; with the ledger `status` indexes the saving
is 737 MB (7 %). The case for migration 0009 is storage and write
amplification, not a speed claim. Forward on the 8.3 M-account database: 0.4 s
(indexes already dropped by hand) and the status drop is instantaneous; reverse
(recreating all seven indexes) **30.6 s**. `makemigrations --check` is clean.

## 14. Why WAL grows, and the tuning levers that did not help here

`pg_waldump --stats` over 667 MB of WAL written by one import at 7.6 M accounts:
**84 % of WAL bytes are full-page images (FPI)**, 69 % of all WAL is B-tree page
images alone, 32 % heap, 3 % GIN. The random hex keys touch cold index pages, and
each page's first change after a checkpoint is logged as a whole 8 KB page.
WAL per imported account therefore grows with size (6.0 KB at stage 0, 8.9 KB at
stage 2, 17-21 KB per account in 176 k-account imports at 8 M).

| Experiment (176 k-account import into 7.6-8.1 M accounts, no queries) | Accounts/s | WAL | Verdict |
| --- | ---: | ---: | --- |
| baseline `wal_compression=off` (f1) | 1,714 | 3.01 GB | reference |
| baseline again (f3) | 1,835 | 3.65 GB | spread among identical runs: 7 % rate, 10-20 % WAL |
| `wal_compression=pglz` (f2) | 893 | 2.25 GB | WAL -25 %, **rate -50 %**: CPU-bound on 2 vCPU. Do not enable here |
| `checkpoint_timeout=30min` (f4) | 1,841 | 3.42 GB | no effect (checkpoints are triggered by `max_wal_size`, 13 requested checkpoints) |
| `shared_buffers=2GB` after restart (e2, 440 k accounts) | 1,716 | 6.60 GB | **-18 %** versus the surrounding 512 MB runs (2,095 and 1,934): cold buffers, and on a 3.9 GB host less OS page cache |

Not tested (disk limit 21 GB in this sandbox): `max_wal_size` above 4 GB, which
would reduce FPI volume by checkpointing less often. `lz4` WAL compression is
not compiled into this PostgreSQL build, so it could not be tried; on a
multi-core host it is the first experiment to run.

## 15. Search totals: exact versus capped, and cached whole-table totals

Why: `EXPLAIN (ANALYZE)` of a broad search on 7.4 M accounts shows the page
query (`ORDER BY created_at LIMIT 100`) takes **0.5-2 ms** and the exact
`COUNT(*)` takes **1.0-2.1 s** (a parallel hash join over the whole foreign-key
index). The count is the entire cost of a broad search.

`manage.py perfbench counts` (raw: `counts_7m.json`, `counts_8m.json`). URL substring searches at **8.27 M accounts**, p50 of 3 runs, ms:

| Pattern | Matches | Exact count | Capped count, cap 10 k | cap 100 k | Whole view exact | Whole view, cap 100 k |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `example` | 8,271,888 | 1,016.7 | 2.1 | 9.8 | 1,094.9 | **45.1** |
| `host1.example` | 4,415,014 | 157.1 | 10.7 | 16.0 | 215.4 | 66.0 |
| `login/1` | 4,590,837 | 661.8 | 2.0 | 12.7 | 657.9 | 44.0 |
| `login/12` | 459,311 | 82.3 | 2.6 | 26.2 | 200.8 | 123.5 |
| `login/123` | 45,927 | 19.9 | 13.1 | 20.8 | 78.4 | 81.9 |
| `login/1234` | 4,557 | 2.7 | 2.5 | 3.1 | 70.0 | 66.8 |
| `login/12345` | 424 | 0.9 | 0.8 | 1.1 | 38.0 | 37.3 |
| no match | 0 | 0.1 | 0.2 | 0.2 | 46.0 | 44.8 |

The bounded count (`SELECT COUNT(*) FROM (... LIMIT cap+1)`) is at most 26 ms
for every selectivity tested, never slower than the exact count by more than
noise, and **exact for any search with at most `cap` matches**. A broad search
page drops from 1.1 s to 45 ms. It is **opt-in** (`VAULT_RESULT_COUNT_CAP`,
default 0 = exact), the page then shows `100,000+` and a note, exports and
cursor paging still reach every match. Planner risk: a bounded count could pick a
poor plan for a sparse match set; none appeared in the eight selectivities
above on this data's skew, which is only one distribution.

It does **not** help the keyword search (`substring_keyword`, 5 reference
tables): its cost is resolving matching reference ids through a broad trigram
bitmap (about 0.5 s at 7.4 M with the cap on), which this change does not touch.

Whole-table totals (`dashboard`, `Records`, `References`, 7.4 M accounts, view
time p50 of 5 calls): dashboard **575 -> 34 ms**, Records without a query
**189 -> 3.5 ms**, References index **545 -> 32 ms**, one reference list **150 ->
26 ms** with the default 60 s cached total (`table_total`), versus
`VAULT_TOTAL_COUNT_TTL=0`. The count is still exact when taken: tables below 1 M
estimated rows are counted live.

## 16. Fast mode re-test with captured errors (880 k accounts, 200 k-line import, 2 query workers)

Each variant on its own clone of the same database (`raw: online_880k.json,
fast_search_880k.json, fast_all_880k.json`):

| Variant | Import time | Accounts/s | WAL | Broad URL substring p50/p95 during | Exact email p50 during | Query errors |
| --- | ---: | ---: | ---: | --- | --- | ---: |
| online (default) | 39.7 s | 4,435 | 685 MB | 183 / 323 ms | 10.8 ms | 0 |
| `--fast`, scope `search` (trigram indexes suspended) | 72.0 s | 2,449 | 744 MB | **951 / 1,298 ms** | 9.7 ms | 0 |
| `--fast`, scope `all` | 79.9 s | 2,206 | 706 MB | 730 / 971 ms | **505.5 ms** (52x) | 0 |

Fast mode is **1.8-2.0x slower than the default** here and degrades reads, so
the earlier recommendation stands: do not use it for incremental loads. The
`OperationalError` seen once in the first fast run was **not reproduced** in
these three runs with the exception text now captured (400 characters); its cause
remains unexplained.

## 17. PostgreSQL server crash drill

`benchmarks/pg_crash_drill.sh`: a durable import of the 1 M-line / 8-file
folder into an empty database; at 35 s `pg_ctl stop -m immediate` (no shutdown
checkpoint, connections cut), restart, `pg_amcheck --heapallindexed`, then
`manage.py recoverimports --run`. Two runs (kill at 60 s and 35 s):

| | Run 1 | Run 2 |
| --- | ---: | ---: |
| Durable at the crash | 306,262 accounts / 348,110 ledger rows | 153,079 accounts / 174,119 ledger rows |
| Server restart with WAL redo | 14.9 s (redo 13.4 s) | 9.6 s (redo 5.6 s) |
| `pg_amcheck` before recovery | clean | clean |
| `recoverimports --run` (replays the remaining positions) | 91 s | 120.5 s |
| Final accounts / ledger | 879,519 / 1,000,000 (committed 879,519, skipped 120,481) | the same |
| Duplicate fingerprints; committed rows without an account | 0; 0 | 0; 0 |
| Final `pg_amcheck` | clean | clean |
| Second `recoverimports` | n/a | "No recoverable import jobs." |

Counts equal an uninterrupted run exactly (879,519 / 120,481). Re-running
`importfolder` after recovery was shown to be unnecessary and wasteful (it
re-reads all 1 M lines as duplicates, 100 s, and adds 1 M `skipped` ledger rows); the
drill script no longer does it. **Not covered:** power loss with an OS or disk that
loses acknowledged writes, a full filesystem, torn pages on failing hardware.

## 18. Physical backup and point-in-time recovery drill

`benchmarks/backup_restore_drill.sh` on a cluster with WAL archiving:
`pg_basebackup` (1.8 GB, 5.5 s), then a 200 k-line durable import, then the time
`T_good`, then a simulated operator error (50,000 accounts and their ledger rows
deleted), then restore the backup into a new directory, replay archived WAL to
`T_good`, promote.

| | Result |
| --- | --- |
| Restored copy at `T_good` | 1,181,661 accounts / 1,317,165 ledger rows, **equal to the values recorded at `T_good`** |
| Duplicate fingerprints | 0 |
| `pg_amcheck --heapallindexed` | clean |
| Restore + WAL replay + promote | 15.1 s |

A first attempt reported the pre-import counts only because the script read the
restored cluster while it was still replaying WAL (read-only connections open
before recovery ends); the script now waits for `pg_is_in_recovery()` to be false.
One run, small data; restore time at terabytes is not measured.

## 19. Partitioning prototype (scratch schema, 1 M rows per table)

`benchmarks/sql/partition_prototype.sql` (output `partition_prototype.txt`). The
ledger and the account table copied from real rows into plain and 16-way hash
partitioned tables with the same indexes, then +500 k ledger rows. Everything fits
in cache at 1 M rows, so this shows the **cost** of partitioning on one node, not
the large-scale benefit it is meant to give.

| Measure | Plain | Hash partitioned x16 |
| --- | ---: | ---: |
| Ledger 1 M rows load | 12.8 s | 12.5 s |
| +500 k ledger rows | 7.9 s | 8.6 s (+9 %) |
| 20,000 key lookups, key only | 365 ms | **1,026 ms** (2.8x; all 16 partitions probed) |
| 20,000 key lookups with the job id | n/a | 107 ms (pruned) |
| Per-job status aggregate (1.5 M rows) | 499 ms | 541 ms |
| `VACUUM (FREEZE)` | 2.4 s | 2.3 s |
| `REINDEX` unique key | 1.4 s | 2.2 s |
| Ledger size | 495 MB | 518 MB (+5 %) |
| Account table size | 303 MB | 382 MB (+26 %; composite primary key) |
| `count(*)` of 1 M accounts | 66.8 ms | 55.1 ms |
| `UNIQUE (id)` on accounts partitioned by `fingerprint` | allowed | **refused by PostgreSQL** (unique constraints must contain the partition key), so the ledger's `account_id` foreign key cannot reference it |

Conclusion (R8): no gain on one node at this size; partitioning the account
table would break the existing foreign key; the ledger could be partitioned
with a `(import_job_id, idempotency_key)` unique key, but only an archive/drop
benefit (per-job partitions) would justify it, which needs a retention policy
(R6). Nothing was applied to the schema.

## 20. Import metrics (new)

`vault/import_metrics.py`, wired into `DurableImportSession`: per job batches,
items, valid rows, items/s, database seconds, `db_busy_ratio`, batch time
p50/p95/max, batch bytes and the parse-queue backlog (current and peak), logged
as one `vault.import.metrics` line every `VAULT_IMPORT_METRICS_INTERVAL` seconds
(30; 0 disables) plus a summary. Sample from the 200 k-line import into an empty
database (batch 50 k): 8,543 items/s, `db_busy_ratio` 0.909, batch p50 5.4 s, parse
backlog 10.4 MB (bounded), i.e. database-bound; a stall of the parser would show as
a low `db_busy_ratio`. A test asserts that the log contains no source value,
host, email, IP or password.

## 21. Test status

Full `manage.py test --parallel=1` after these changes: 261 tests OK,
5 skipped (the opt-in performance tests, run separately). See
`docs/scalability/IMPLEMENTATION_AUDIT.md`.

## 22. Defects found by re-running the opt-in performance tests

Re-running `ConcurrentImportSearchTests` followed by `FileConcurrencyBenchmarkTests`
(`VAULT_PERF_LINES=50000`) in one process, as the README documents, exposed two
defects in the importer that the 259-test default suite did not.

**22.1 Deadlock between parallel file imports.** PostgreSQL logged `deadlock
detected` on `INSERT INTO vault_ip ... SELECT DISTINCT ip ... ON CONFLICT (name) DO
NOTHING`, run by two of the four `--parallel-files 4` jobs; one job failed with
`OperationalError` (recoverable with `recoverimports`; no data lost). Cause: each
transaction inserts the same unique names in the planner's `DISTINCT` order (hash
order), so two transactions can each hold a name the other waits for. Fix: every
reference and account `INSERT ... SELECT` is now `ORDER BY` the unique key, so waits
always point the same way. Evidence: 2 of 2 failing runs before (7 `deadlock
detected` entries in the server log while the experiments ran), none in the 4 runs
after. The unit-level reproduction attempt (3 further runs, ordering removed)
did produce server-side deadlocks but did not fail the smoke test reliably, so the
automated guard is structural (`InsertOrderTests`, SQL contains `ORDER BY ... ON
CONFLICT`), not behavioural. Cost, measured: on a 1.13 M-account database, a 500,000-line (439,862-account)
durable import took 79.5 s and 79.0 s with ordering versus 81.2 s and 81.4 s without
(5,550 versus 5,410 accounts/s, about 3 % faster; WAL 2.178 versus 2.185 GB, -0.3 %;
two alternating runs each on clones of the same database,
`results/2026-10-19/ordering_ab_import.txt`). The effect at 5-8 M accounts, where
the index working set exceeds RAM, was not measured. Also no cost visible in the 50 k-line runs (parallel-files
2: 10.0-10.6 s before and after; 7.6-8.0 k lines/s at 1 file).

**22.2 Runaway account INSERT.** In one run the account `INSERT ... SELECT` ran for
25 minutes at 100 % CPU on a 50,000-line batch (cancelled manually). A reproduction
captured the plan: `vault_url` and `vault_pass` were estimated at 1 row, so the join
was a nested loop that rescanned the 50,000-row `vault_url` for every staged row
(O(n^2)). Cause (reproduced in a scratch database): a table analyzed while empty
(1 page, `reltuples = 0`) keeps estimating one row however large it later grows, and
ANALYZE cannot see rows inside an open transaction. Fix:
`refresh_stale_reference_stats` runs `ANALYZE` on the joined reference tables only
when `reltuples <= 0` and the table is at least 16 pages, inside the batch
transaction; healthy tables are never touched, so large databases pay nothing. After
the fix, 4 of 4 runs of the failing sequence completed (1 file: 12.5-12.8 s; 2 files:
10.0-10.6 s; 4 files: 9.7-11.2 s for 100,000 lines). With the ordering fix already in place, the hang occurred in 2 of 4 combined runs
before this fix and in 0 of 4 after: that is evidence, not a proof;
the plan-shape cause was captured directly and confirmed in isolation. The same
state can occur in production after a purge followed by an autovacuum analyze of the
emptied tables, so this was a real, not only a test-harness, risk.

Raw: `results/2026-10-19/perftest_file_concurrency_before_fix.log`,
`perftest_after_fix_run4.log`.

## 23. Defect reported from real use: upload import failed when the browser navigated away

Report (18,212,968-line upload, Windows, `runserver`): the job ran at about 8,000
lines/s, `db_busy_ratio` 0.80-0.82 and `parse_pending_bytes` 0 (so the database, not
the parser, was the limit, as in section 12), then at 11.1 % (527,719 accounts
committed, 2,027,733 lines attempted) it failed with
`SourceChangedError` 28 s after the user left the import page for `/import/jobs/`.
Not a performance problem and not a file change. Cause, reproduced by
`tests/tests_upload_disconnect.py` (before the fix: job `failed`, same log line):
the import runs in a worker thread inside the streaming HTTP response; when the
client disconnects the server closes the response and Django closes `request.FILES`,
deleting the temporary upload while the worker still reads it. A second defect in
the same path (introduced by the bounded progress queue of the first pass): with no
consumer left the worker would block on `put()` after 64 queued events. Fix: the
response generator now waits for the worker in a `finally` (so the upload stays open
until it is read), and progress events are dropped instead of blocking once the client
is gone. After the fix the same test completes all 3,000 lines. Not covered: the
client-side script, a window closed before the job exists, a server restart (uploads
are never replayable by design); the 18 M-line case itself was not re-run here (it
needs the user's file). The reported run's 74 % skipped lines were not examined here.

## 24. Field run on Windows after PostgreSQL tuning, and the cost of blank lines

**A. User-reported run (not measured by me).** Same 18,212,968-line source, same
machine (Windows, 32 GB, 5 cores, SSD, PostgreSQL 18.6, `VAULT_IMPORT_WORKERS=3`,
empty database both times), copied from the user's `vault.import.metrics` log lines.

| | PostgreSQL defaults (`shared_buffers` 128 MB, `max_wal_size` 1 GB) | After `shared_buffers=4GB`, `max_wal_size=32GB`, `checkpoint_timeout=30min`, `wal_buffers=64MB`, `effective_cache_size=22GB`, `maintenance_work_mem=2GB`, `random_page_cost=1.1` |
| --- | --- | --- |
| Lines/s between log lines | 8,531, then ~6,900 | 14,480 early; 10,400-13,400 from 3 M to 6 M lines (11,969 in the last interval) |
| Median batch time | 10 s rising to 17-25 s | 10.0 s rising slowly to 12.1 s at 6.1 M lines |
| `db_busy_ratio` | 0.88 to 0.93 | 0.88 to 0.91 |
| Time to ~2.4 M lines | 259 s | 165 s |

Single runs, one machine, run still in progress when reported (6.08 M of 18.2 M lines,
1.61 M accounts). The DB is still the limiting stage (parse queue at its cap). Only the
combination of settings was changed, so the table does not say which one mattered.

**B. Skipped lines (user-reported breakdown + `explainrejects`).** Skipped lines were
96.1 % `invalid_format`, 3.9 % duplicates, 5 `line_too_large`. Sampling every 10th line
of the first ~3 M lines of the file: 25.9 % records, **64.6 % blank lines**, ~9 % other
rejections (mostly `:`-separated lines whose password/IP field or URL fails the format check).

**C. Cost of blank lines (sandbox, measured).** Every line gets a ledger row, including
blank ones. 300,000 generated lines (4 % duplicates, 9 % invalid), imported into an empty
database with `VAULT_IMPORT_WORKERS=1`, once as-is and once with blank lines interleaved
to reach 64.5 % blank lines (545,755 blanks). Same 261,138 accounts both times.

| Input | Lines | Ledger rows | Seconds |
| --- | ---: | ---: | ---: |
| No blank lines | 300,000 | 300,000 | 40.7 |
| 64.5 % blank lines | 845,755 | 845,755 | 51.7 |

Blank lines added 27 % to the run time (~20 us per blank line against ~136 us per
non-blank line). One run each, 2 vCPU. **Projection, not measured on the user's
machine:** removing blank lines before import would shorten a file like theirs by
roughly a fifth. The audit ledger would then hold no row for those lines. Counting
blank lines without a ledger row inside the importer was not implemented because it
changes the audit, resume and verify semantics.
