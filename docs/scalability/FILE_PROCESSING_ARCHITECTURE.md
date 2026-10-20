# File-processing architecture (deliverable C)

Status legend: **[existing]** was in the repository at the start; **[new]**
implemented in this program and tested; **[measured-no]** evaluated and
deliberately not adopted; **[future]** proposal only.

## 1. Pipeline

```
discover -> manifest (names, sizes, SHA-256, line count) -> open
  -> bounded line reader -> [parse workers: decode, split, validate, normalize, hash]
  -> bounded batch buffer -> one transaction per batch:
       COPY stage -> reference upserts -> account INSERT..SELECT (unique fingerprint)
       -> ledger COPY + INSERT -> counters + checkpoint + lease fence
  -> commit (synchronous_commit = on) -> visible to readers
```

| Stage | Implementation | Status |
| --- | --- | --- |
| Discovery | `collect_files_from_path`, sorted, extension filter | [existing] |
| Source identity | manifest of ordered relative names, byte sizes and digests; revalidated before import, at batch boundaries and before completion; a changed source is refused | [existing] |
| Reading | binary/text `readline` with a 16 KiB cap; an overlong line is consumed in 64 KiB pieces and represented only by its SHA-256 and length | [new] `vault.utils.iter_bounded_source_lines` |
| Parsing | `vault.workers._parse_durable_chunk` in forked/spawned processes; pending work bounded by estimated bytes | [existing] + [new] byte bounds |
| Batching | flush when valid rows, outcome rows or estimated buffer bytes reach their limit | [existing] + [new] byte threshold |
| Dedup | in-batch by position/hash, then authoritative `ON CONFLICT (fingerprint) DO NOTHING` | [existing] |
| Persistence | `COPY` into temp tables + set-based SQL | [existing] |
| Visibility | one atomic commit per batch (accounts, ledger, counters, checkpoint) | [existing] |

## 2. Reading strategy and why not segmentation

The line format (`URL<sep>identifier<sep>password[<sep>IP]`, newline-delimited,
no quoting, no embedded newlines) means a record boundary is exactly a `\n`.
Stage profiling (`docs/scalability/BENCHMARK_RESULTS.md` section 1) shows reading and
line-splitting cost 2-3 % of the pre-database work and parsing 92 %. Therefore:

* **Sequential streaming per file** is the adopted strategy for every file size.
  A single reader never holds more than one bounded line plus the batch buffer.
* **Parallelism is applied to parsing** (worker processes), where the time is.
* **Intra-file byte-range segmentation [measured-no].** A segment worker would
  have to scan to the first `\n` after its offset and to finish the line that
  crosses its end; that is correct for this format, but it parallelizes a
  2-3 % stage while breaking the gap-free per-line positions that the ledger,
  idempotency keys and recovery depend on (positions would need a prefix-sum
  of line counts per segment, i.e. a pre-pass). Not implemented.
* Memory mapping, vectorized parsing and zero-copy were not adopted: the read
  stage is already 5.7 GB/s warm and CPU time is in Python validation. Cold-cache
  and slower-storage behaviour were **not measured**; revisit if a deployment
  reads from network storage.
* **Encoding.** Files are read as bytes and the field normalizers decode them as UTF-8 (invalid sequences are dropped, existing behaviour); the manifest hash is
  over the exact bytes, so a changed byte is detected. UTF-8 multibyte
  sequences cannot contain `\n`, so splitting on `\n` cannot cut a character.
  Byte-order marks and non-UTF-8 encodings are **not** detected specially
  (unchanged behaviour). Compressed inputs are not supported (the file filter
  excludes common archives); a compressed stream would be read sequentially.
* **Malformed / incomplete input.** A final line without `\n` is processed as a
  record; blank lines, `#` comments and box-drawing decoration are skipped; a
  line that does not match the record shape is a `skipped` ledger row with an
  `invalid_format` category and a source hash only (no plaintext).

## 3. Concurrency model and unified budget

| Level | Control | Default | Hard limit | Evidence |
| --- | --- | --- | --- | --- |
| Parser processes per job | `VAULT_IMPORT_WORKERS` | `min(4, CPUs)` | 16 | parse is 92 % of CPU work |
| Pending parse chunks | derived from workers | `3 x workers` | 16 chunks / 64 MiB | backpressure: the reader stops submitting |
| Batch buffer | `VAULT_IMPORT_BATCH_ROWS` | 250,000 rows | 1 M rows, 256 MiB | 250 k was fastest of 10 k/50 k/250 k |
| File-level jobs | `importfolder --parallel-files N` | 1 | 8 | mixed on 2 vCPU: +29 % into an empty database, -4 to -11 % into a populated one (results section 6) |
| DB writers | one transaction per job at a time | per job | n/a | lease fences concurrent owners |
| Web progress stream | bounded queue of 64 events | 64 | n/a | slow client cannot grow memory |

When `--parallel-files` is greater than 1, each file job gets exactly one
parser process, so total parser processes equal the file concurrency instead
of multiplying (this is the single "budget": parser processes x jobs <= 8).
The source side uses a bounded submission queue and per-thread DB connection
cleanup.

**Adaptive concurrency [measured-no].** The results show that more concurrency
helped in one regime and hurt in another on the only host measured (2 vCPU). A controller that grows
workers on observed gains would need multi-core measurements to be meaningful;
without them it would be speculative code. Static, explicit, bounded settings
are used, which also keeps runs reproducible. Queue depth and DB latency are
not exported as metrics yet **[future]**; today the progress stream and the job
page expose committed/skipped/failed counters and stage.

## 4. Identity, checkpoints and recovery

* **Source identity [existing]:** per-job manifest digest; per-line
  `idempotency_key = sha256(job_id:position)[:64]` (unique); `source_hash` is the
  SHA-256 of the raw line (the plaintext line is not stored for new imports).
* **Checkpoint [existing]:** a gap-free terminal prefix of positions, committed
  in the same transaction as the accounts and ledger rows of that batch.
* **Lease fencing [existing]:** only the owner of a live job-row lease may commit
  or release; a stale worker raises `LeaseLostError` and cannot overwrite a
  replacement.
* **Recovery [existing + new CLI]:** `recover_job` re-reads the source,
  validates the manifest, and replays only positions without a terminal ledger
  row, keeping their original positions and keys. `manage.py recoverimports`
  [new] is the command-line equivalent of the Resume button.
* **Retry safety:** a repeated delivery of the same position hits the unique
  idempotency key; a re-inserted account hits the unique fingerprint.
  `test_kill_after_commit_before_ack_resumes_without_duplicates` and the SIGKILL
  experiment (81,582 lines durable at kill, 118,418 replayed in 23.3 s, final
  counts identical to an uninterrupted run, 0 duplicate fingerprints) cover it.
* **Reference cache [new bound]:** the per-job "already stored" cache that
  avoids re-issuing reference upserts is cleared at 200,000 values per kind and
  whenever a batch transaction fails. Previously it grew with every distinct
  value for the life of the job (including credentials held in memory) and was
  not invalidated on rollback; correctness always rests on the unique
  constraints, never the cache.

## 5. What is durable, reconstructable, or lost on a crash

| Item | After a crash |
| --- | --- |
| Every committed batch (accounts, reference rows, ledger rows, counters, checkpoint) | durable (`synchronous_commit=on`); atomically all or nothing |
| An uncommitted batch (up to `batch_rows` valid lines plus pending skipped lines) | lost from the database; recomputed from the source on recovery |
| Skipped/failed outcomes buffered since the last commit | lost, but reconstructed by replay because they are derived from the source |
| Uploaded (non-path) sources | not retained; unaccounted lines of an interrupted upload cannot be replayed automatically (existing limitation) |
| Counters | recomputed from the ledger by recovery |

## 6. Failure handling summary

| Failure | Behaviour | Tested |
| --- | --- | --- |
| Process killed after a commit | recovery replays the rest, no duplicates | yes (unit + SIGKILL experiment) |
| Source file edited during import | refused (`SourceChangedError`) | yes (existing tests) |
| Overlong line | hash-only skipped outcome, bounded memory | yes |
| Worker connection leak in threaded jobs | connections closed per source worker | yes |
| Rolled-back batch with reference cache | cache cleared | yes |
| PostgreSQL restart, disk full, replica failover, backup restore | not tested | no |
| Retry storm protection | a live lease blocks a second owner; no automatic retry loop exists | lease tests (existing) |

## 7. Additions from the second pass (2026-10-19)

* **Insert order.** Every reference and account `INSERT ... SELECT ... ON CONFLICT`
  is sorted by the unique key (`name`, `fingerprint`). Without it, concurrent file jobs
  inserted overlapping keys in hash order and deadlocked
  (`docs/scalability/BENCHMARK_RESULTS.md` 22.1). Guard: `tests/tests_scale_changes.py`
  (`InsertOrderTests`).
* **Planner statistics.** A reference table analyzed while empty keeps `reltuples = 0`
  and is estimated at one row however large it grows, turning the account INSERT's
  joins into an O(n^2) nested loop. `refresh_stale_reference_stats` analyzes such tables
  inside the batch transaction; healthy tables are never touched (22.2).
* **Upload lifecycle.** An upload import runs in a worker thread inside the streaming
  response. The response generator waits for that worker in a `finally`, so Django
  cannot delete the temporary upload (`request.FILES`) while it is still being read
  when the browser disconnects, and progress events are dropped instead of blocking
  once nobody is listening (section 23 of the results; `tests/tests_upload_disconnect.py`).
  Uploads are still never replayable after a restart: use `importfolder` for large
  files.
* **Windows.** Parse workers default to one there (spawn start method); raise
  `VAULT_IMPORT_WORKERS` explicitly.
* **Metrics.** `vault/import_metrics.py` logs counters and timings only (no source values).
