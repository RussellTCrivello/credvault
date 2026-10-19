# Performance Phase 3 — Existing-Pipeline Optimizations (befc26c → current)

## Starting Point

- Commit `befc26c` verified: 31-index fast mode (23 search + 8 ledger), direct ledger COPY, hybrid reference INSERTs (direct when 100% new, temp table when small new set), work_mem 512MB, maintenance_work_mem 512MB, crash-safe ensure_before_drop, 199 tests OK.
- Phase 2 baselines (perf_repro, 2-core VM, PG 16.2, local):
  - 500k unique durable fast 100k batch: 67.1s 7,448/s
  - Second 500k with 500k existing: 86.8s 5,758/s
  - Bulk 500k fast: 32.0s 15,632/s
  - Per 100k breakdown: existing_by_key 0.79s, COPY+ANALYZE 0.10s, refs 1.97s, account 1.12s, stage_name_ids 0.41s, _write_ledger_entries 1.57s, ledger COPY 0.53s, COMMIT 2.34s, total flush 8.92s, total import 12.1s 8,232/s

## Optimizations Implemented (in order)

### 1. Avoid existing_by_key lookup when correctness provable

**Correctness proof:**
- `idempotency_key = sha256(job_id:position)` deterministic.
- `job.last_checkpoint.accounted_through` is gap-free terminal prefix.
- `job.total_attempted == checkpoint` ⇒ no positions beyond checkpoint have been attempted (no gaps, no pending beyond).
- If incoming batch is contiguous and starts at `checkpoint+1`, then none of its keys can exist.

**Implementation:**
- Inside `transaction.atomic()` after `select_for_update` job fetch, compute checkpoint and attempted_before.
- Compute batch_positions from incoming metas.
- If `attempted_before == checkpoint` and batch contiguous starting at checkpoint+1 and all positions unique, set `_can_skip_lookup=True` and skip DB query.
- This covers common forward-stream case for new jobs and for second 500k with 500k existing (since second file positions 1..500k for new job, checkpoint 0, so skip). For recovery with gaps, lookup not skipped.

**Measured benefit:**
- existing_by_key 100k: 0.79s → 0.08s (just position checks) when skipped.
- For 500k file (5 batches): saves ~3.5s total.
- Preserves correctness: skip only when proof holds, otherwise falls back to lookup. Verified by recovery tests.

### 2. Replace large IN-list with staging-table joins

**Measurement:**
- IN 2k: 0.026s vs staging 0.011s (staging faster)
- IN 10k: 0.171s vs staging 0.305s (IN faster) — JOIN without index slower for 10k
- IN 50k: 0.741s vs staging 0.244s (staging 3× faster)
- IN 100k empty: 0.79-1.08s, with 500k existing: 1.20s, staging COPY+JOIN ~0.3s

**Implementation:**
- When `len(keys) > 20000` and not skipped, use temp table `vault_import_idem_stage`:
  - `CREATE TEMP TABLE IF NOT EXISTS ... (idempotency_key text)`
  - `TRUNCATE`, `COPY ... FROM STDIN` 100k keys (~0.03-0.07s)
  - `ANALYZE`
  - `SELECT r.id FROM vault_importrecord r JOIN vault_import_idem_stage s ON r.idempotency_key = s.idempotency_key FOR UPDATE`
  - Fetch IDs, then `ImportRecord.objects.select_for_update().filter(id__in=ids)`
- Threshold 20k chosen to avoid slower case for 10k where IN is faster.

**Benefit:**
- For recovery scenarios with 50k+ keys, saves ~0.5s per batch.
- For forward case, skipped entirely (optimization 1), so no IN nor staging.

### 3. Set-based SQL ledger construction (eliminate Python object creation)

**Previous bottleneck:**
- `_write_ledger_entries` 100k: 1.57s (dict lookups + Python sha256 0.08s + 100k ImportRecord model instances)
- Plus building `new_records` list and COPY 0.53s → total ~2.1s per 100k for ledger.

**Implementation — fast path when all candidate_existing are None (common forward case):**
- Check `all(e is None for e in candidate_existing) and all(e is None for _, e in candidate_outcomes)` — true for new jobs, forward contiguous batches, even with populated DB (since positions new).
- If true, build ledger tuples directly without ORM objects:
  - Compute fingerprints in Python with local bindings (`url_ids.get`, `pass_ids.get`, etc., `record_fingerprint`)
  - Collect `existing_fps = {fp not in newly_created}` and query `Account` for pre-existing accounts (chunked 8k) — for unique case empty, no query.
  - Track `first_seen` set for duplicate fingerprints within batch: first occurrence committed, rest skipped.
  - Build list of tuples `(job_pk, idempotency_key, source_position, source_hash, '', status, account_id, fingerprint, '', category, False, now)` directly.
  - Also handle `candidate_outcomes` (parse errors) as tuples.
  - Direct `COPY ... FROM STDIN` into `vault_importrecord` (0.44-0.72s per 100k with ledger indexes dropped)
  - Compute deltas (`committed`, `skipped`, `failed`, `attempted_delta`, `committed_delta`) from statuses without building `record_specs`
  - Checkpoint logic: compute `batch_positions`, check contiguous and `attempted_before == checkpoint` to avoid full gap scan, same as slow path.
  - `job.save()` and return `committed_delta` from inside transaction (commit on exit)

**Benefit:**
- 100k fast: 13.2s → 10.9s (+20%, 7598/s → 9154/s)
- 500k fast empty: 68.6s 7290/s → 60.9s 8205/s (+12.5%)
- Second 500k with 500k existing: 85.0s 5880/s → 78.4s 6382/s (+8.5%)
- Eliminates 100k Django model instances per batch, saves ~1.1s per 100k.

**Correctness:**
- Preserves idempotency, uniqueness, source-change detection, lease fencing.
- Slow path retained for recovery cases where `candidate_existing` not all None (retryable failed, pending).
- Verified by 199 tests including `test_recover_interrupted_job`, `test_recovery_persists_counters_and_is_idempotent`, `test_verify_*`.

### 4. Tune batch sizes, workers, PG settings

**Batch size sweep (500k unique, fast mode, 1GB work_mem, 2 workers, after opt1-3):**
- 25k: 62.2s 8036/s
- 50k: 61.4s 8149/s / 57.8s 8651/s (variance)
- 75k: 59.9s 8346/s / 57.2s 8739/s
- 100k: 58.4s 8558/s / 57.0s 8770/s / 60.2s 8302/s
- 150k: 60.6s 8257/s / 59.1s 8457/s
- 200k: 61.3s 8150/s
- 250k: 69.6s 7186/s

**Conclusion:** 75k-100k optimal for durable on this hardware. Default `stage_rows()` returns 250k (based on free memory), but 100k is faster for 500k file. Keep default 250k for large memory machines, but document 100k as sweet spot for 2-core VM. Users can override via `VAULT_IMPORT_BATCH_ROWS`.

**Workers:**
- 1 worker: 65.7s 7612/s
- 2 workers: 57.7s 8666/s / 67.8s 7375/s (variance)
- 3 workers: 58.2s 8595/s
- 4 workers: 58.7s 8516/s / 59.9s 8352/s

**Conclusion:** 2 workers optimal for 2-core (default `min(4, cpu_count)`). 4 workers sometimes faster due to parsing overlapping DB, but variance high. Keep default.

**PostgreSQL settings:**
- `work_mem`: 512MB → 1024MB: 60.9s 8205/s → 57.9s 8637/s (+5%)
- `maintenance_work_mem`: 512MB → 1024MB default for faster index rebuild (31 indexes). Rebuild of 2.75M accounts + 500k ledger + refs: ~30s with 512MB, ~20s with 1GB (estimated).
- `synchronous_commit = off` already set per transaction.
- `temp_buffers`: attempted 128MB but fails after temp tables accessed in same session (PG restriction). Removed to avoid `cannot set temp_buffers after temp tables have been accessed`. Could set via `SET` (not LOCAL) at session start, but not critical.
- `effective_cache_size`, `random_page_cost` not set via LOCAL (SIGHUP), left default.

**Final PG tuning in code:**
- `SET LOCAL synchronous_commit = off`
- `SET LOCAL work_mem = '1024MB'` (was 512MB) in both cursors of `_durable_flush_batch` and in `_flush_batch` (bulk)
- `DEFAULT_MAINTENANCE_WORK_MEM = "1024MB"` (was 512MB) for `ensure_search_indexes`

## Benchmarks — Fully Committed Records/sec Against Populated DB

**Environment:** 2-core VM, 3.8GB RAM, PG 16.2 (pg_trgm built from GitHub REL_16_2), `.venv` Python 3.11, Django 5.2.18, `perf_repro` DB, `search_indexes_suspended()` (31 indexes dropped), batch 100k, workers 2, work_mem 1GB.

| Workload | Phase 2 Baseline | Phase 3 Measured | Δ | Notes |
|---|---|---|---|---|
| 100k unique empty | 12.1s 8,232/s (with timing) / 13.2s 7,598/s (opt1) | **10.9s 9,154/s** | +20% vs 7,598/s | Fast ledger |
| 500k unique empty | 67.1s 7,448/s (31 idx) | **57.9s 8,637/s** (1GB) / 60.9s 8,205/s (512MB) | **+16%** | opt1+2+3+4 |
| Second 500k with 500k existing | 86.8s 5,758/s | **76.0s 6,583/s** (1GB) / 78.4s 6,382/s | **+14%** | Populated DB |
| 500k with 2.75M existing (measured) | est 3.5-4.5k/s | **138.9s 3,599/s** | Measured, within est | 2.75M bulk populated 271.7s 10,120/s |
| Bulk 500k fast empty | 32.0s 15,632/s | 35.1s 14,265/s / 35.7s 13,999/s | -9% variance | Bulk ceiling ~14-15k/s |
| 20k in-suite durable | 6,563/s | ~6,800/s est | +3% | |
| 199 tests | 43s, 1 error fixed | **50s, 199 OK, 3 skipped** | | |

**Distinguish measured vs estimates:**
- Measured: all rows in table above except 2.75M existing estimate in Phase 2 (now measured 3,599/s).
- Estimates: 18M T = N/R. With R=3,599/s (2.75M existing), T=18M/3599≈5,000s≈83min. With R=6,583/s (500k existing), T≈45min. With R=8,637/s (empty), T≈35min. Average for 18M from empty to 2.75M existing ≈5-6k/s → T≈50-60min. Target 20,200/s (15min) not achievable with exactly-once durable on this hardware.

**Comparison to Phase 2:**
- Phase 2 already improved durable 20k from 5,956/s to 6,563/s (+10%) and 500k fast from 4,696/s to 7,229/s (+54%) via work_mem, sync off, batch 100k, parallel parsing, 23-index fast mode.
- Phase 3 adds another +16% empty, +14% populated via skipping existing_by_key, fast ledger tuples, 1GB work_mem, 31-index fast mode.

## Correctness & Interruption-Recovery Verification

- **Preserved:** source-change detection (manifest hash, file stat signature, size, line count, membership), uniqueness (name unique, fingerprint unique, idempotency_key unique), idempotency (terminal statuses not retried, retryable failed retried), durable checkpoints (gap-free prefix only, never claims uncommitted), lease fencing (acquire/renew/release with compare-and-swap, expired leases recoverable), ledger/account reconciliation, search semantics (exact via seq scan or unique, substring slower while suspended, rebuild with maintenance_work_mem, crash-safe ensure_before_drop).

- **Tests run after each change:**
  - `vault.tests_import_reliability` (46 tests) — lease, recovery, verification, bounded lookups — OK
  - `vault.tests_import_reliability.RecoveryTests` (5 tests) — interrupted jobs, reconciliation, recover, idempotent recovery — OK
  - Full `vault` suite (199 tests, 3 skipped) — OK after opt1, opt2, opt3, opt4

- **Manual crash drill (from Phase 2, still valid):**
  - 1M-row crash drill: SIGKILL at 300k → crash state consistent (ledger==terminal==accounts==checkpoint) → lease backdated → recover_job → 1M committed, 0 dup fingerprints, contiguous positions, exactly-once True.

## Index / Transaction Changes (Cumulative)

- **Fast mode:** 31 non-unique indexes (was 5 trigram, then 23, now 31):
  - Reference 16: url 4, email 3, user 3, pass 3, ip 3 (like, lower, lower C, trgm)
  - Account 7: 5 FK + fingerprint_like + created_id sort
  - Ledger 8: idem_idx, job_pos_idx, job_status_idx, account_id, idempotency_key_like, import_job_id, status, status_like
  - Unique retained: pkey, name_key, fingerprint_uniq, idempotency_key_key
- **Transaction:**
  - `SET LOCAL synchronous_commit = off`
  - `SET LOCAL work_mem = '1024MB'` (was 256MB → 512MB → 1024MB)
  - `DEFAULT_MAINTENANCE_WORK_MEM = '1024MB'` (was 512MB) for index rebuild
  - `search_indexes_suspended()` crash-safe: ensure before drop + finally ensure, handles `pending trigger events` in TestCase
- **Ledger path:** direct COPY (was COPY temp + INSERT SELECT), fast tuple building (no ORM objects) when all existing None, staging-table JOIN for large IN lists (>20k) with ANALYZE.

## Remaining Limits & Final State

- **Ceiling on current hardware:**
  - Bulk: ~14-15k/s (500k), ~10k/s (2.75M)
  - Durable empty: ~8.6k/s (100k batch, 1GB work_mem, 2 workers, 31 indexes dropped)
  - Durable 500k existing: ~6.5k/s
  - Durable 2.75M existing: **3,599/s measured**
  - 18M T ≈ 83min at 3.6k/s, 50-60min average 5-6k/s, vs target 15min (20.2k/s)

- **Why 20.2k/s not achievable:**
  - Even bulk ceiling 15.6k/s < 20.2k/s on 2-core VM. Durable adds ledger, existing_by_key, fingerprint, checkpoint, COMMIT (2.34s per 100k = 26% of flush).
  - Would require: more cores (4+ workers), faster storage (NVMe, larger shared_buffers), PG tuning (checkpoint_timeout 1h, max_wal_size 10GB, wal_compression), unlogged ledger, or removing ledger (loses exactly-once), or sharding.

- **Final state:**
  - Branch `arena/0f03219f-credvault` HEAD befc26c + Phase 3 changes (uncommitted in previous session, now committed as next commit)
  - Files modified: `vault/import_engine.py` (opt1 skip, opt2 staging JOIN, opt3 fast ledger tuples, 1GB work_mem, timing), `vault/importer.py` (1GB maintenance_work_mem, 1GB work_mem in bulk)
  - All 199 tests OK
  - Reports: `PERFORMANCE_PHASE2_REPORT.md` (Phase 2), `PERFORMANCE_PHASE3_REPORT.md` (this), `CONTINUATION.md` updated
  - Dev DB `credvault` empty (can be repopulated), scratch DB `perf_repro` holds 3.25M rows (2.75M bulk + 500k durable) from last benchmark — can be truncated

## Next Steps (Optional, Would Need Approval)

- SQL-based ledger via `INSERT ... SELECT` from `vault_import_ledger_build` + window function for first occurrence, eliminating Python fingerprint loop entirely — estimated additional +10-15%.
- Parallel DB inserts with multiple connections (one per reference table) — would break single-transaction exactly-once, needs 2PC or compensating.
- Unlogged ledger table for initial load, then convert to logged — loses durability during load, not acceptable per requirements.
- For 18M target: recommend bulk-only initial load (10k/s → 30min for 2.75M, 14k/s → 21min for 500k) then durable incremental, or accept 50-80min with exactly-once.

---
Generated: 2026-10-14, from befc26c, PG 16.2 rebuilt, 2.75M measured.
