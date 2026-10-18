# Before / after results (deliverables E and F)

All numbers were measured on 2026-10-18 in one sandbox: 2 vCPU, 3.9 GB RAM, one
virtual disk, PostgreSQL 16.2 (Unix socket, `shared_buffers=512MB`,
`fsync=on`, `synchronous_commit=on`, `wal_compression=off`), Python 3.11.2,
Django 5.2.18, synthetic data from `perfbench generate`. Reproduction steps:
`benchmarks/README.md`. Raw JSON/console output: `benchmarks/results/2026-10-18/`.

**How to read this.** Each row compares variants inside one run set on the
same cloned database. Most are single runs on a shared VM; where a variant was
repeated, both runs are shown and the spread was 1-3 %. Nothing below is a
production or billion-record claim (see `docs/CAPACITY_REPORT.md`).

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

## 12. Test status

Full `manage.py test vault --parallel=1` after these changes: see
`docs/IMPLEMENTATION_AUDIT.md`.
