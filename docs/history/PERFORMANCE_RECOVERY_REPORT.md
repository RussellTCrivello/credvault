# CredVault — Continuation and Performance Recovery Report (Final)

Date: 2026-10-14 · Branch: `arena/0f03219f-credvault` (base `main` @ `a957402`)
Env: PostgreSQL 16.2 + `pg_trgm` 1.6, Django 5.2.18, Python 3.11, single sandbox VM.
Handoff pointer: `CONTINUATION.md`.

## 1. Exact repository state recovered

- Working tree on `arena/0f03219f-credvault`, base commit `a957402`. No reset.
- Uncommitted changes from stabilization session (D1–D7 fixes) — 199 tests OK (3 skipped), 17 perf OK, live-verified per `FINAL_REPORT.md`.
- Dev DB `credvault` with 2,750,017 accounts + full job history (1M, 1.5M crash-recovered, cancelled).
- `.venv`, PG at `/tmp/pgtest/data`, scratch DBs `perf_repro`, `test_credvault` (dropped freely).
- No `pg_trgm` rebuild needed; cluster intact.

## 2. Previous stopping point (evidence)

Stabilization complete. Next task per directive: performance baselines + bottleneck removal + scale verification + crash drill on optimized path.

## 3. Baselines (before optimization) — measured

| Path | Lines | Time | Rate | Notes |
|---|---|---|---|---|
| Legacy bulk `import_path` (2 workers, no ledger) | 100k | 12.0 s | 8,351/s | `perf_repro` fresh |
| Durable pipeline (product default) | 100k | 30.0 s | 3,338/s | same fixture, `STREAM_BATCH_SIZE=50k` |
| Durable in-suite | 20k | 5.1 s | 3,911/s | `vault.tests_performance` |
| Durable 500k (pre-opt, not run — projected) | 500k | — | ~3,300/s | superlinear risk unknown |

Profile (cProfile 54.8 s + per-statement timers):
- Ledger `bulk_create` param marshalling: 14.7 s (largest)
- Double canonicalization: ~5.7 s validators
- 5× 50k IN-list SELECTs: ~2.6 s wall+marshalling
- Reference INSERTs (~7 s) driven by search-critical indexes (unique, pattern_ops, lower(), GIN trgm) — documented, not a defect.

## 4. Existing recovery mechanisms (audited)

- Source identity: `source_path/name/size/hash` + per-file stat signature + per-line `source_hash`/`source_position` + `SourceChangedError` on mismatch.
- Checkpoint: `last_checkpoint.accounted_through` advances only over gap-free terminal prefix; `last_activity_at`, `recovery_attempts`, `ImportRecoveryLog`.
- Exactly-once: `idempotency_key` unique, `account_fingerprint` unique, atomic batch (accounts+ledger+counters+checkpoint), lease fencing (`worker_id`+`lease_expires_at`).
- Interruption: SIGTERM/SIGINT flush at boundary; hard kill → expired lease → `recover_job` resumes from checkpoint. Previous session verified at 1.5M lines, exactly-once, zero dupes.

## 5. Optimizations landed (all measured, regression-gated)

| # | File | Change | 100k result |
|---|---|---|---|
| 1 | `vault/import_engine.py` | Ledger via temp-table **COPY + INSERT…SELECT** (`_LEDGER_STAGE_DDL/_LEDGER_INSERT`) not ORM `bulk_create` | 3,338 → 4,122/s (+23.5%) |
| 2 | `vault/import_engine.py` | Remove second normalization in hot loop — `parse_line` already canonical (verified identical on 100,009 lines incl. edge cases); delete `_canonical_record`; keep public `canonical_record` in `importer.py` untouched | → 4,744/s (+42%) |
| 3 | `vault/import_engine.py` | Reference ids via set-joins against staged batch (`_stage_name_ids`) not 5×50k IN lists; fallback `_resolve_reference_ids` for direct/test callers | → 4,988/s (+49%) |

- Unused imports cleaned (`Email, Ip, Pass, Url, User` only needed for fallback path — kept).
- Deep-verification test `test_duplicate_fingerprints_resolve_with_one_bulk_account_lookup` updated to pass explicit id maps (intent preserved: single bulk account lookup; old shape asserted 4 queries for IN-list path, new shape 1 query for stage-join path).
- Functional checks after each: mixed valid/invalid/duplicate, NULL account_id ledger rows, idempotent re-import (0 new), email+username types.

**Final suite:** 199 OK (3 skipped) — 52.3 s; perf suite 20k: durable **5,176/s** (was 3,911/s, +32%), bulk **8,290/s** unchanged.

## 6. Scale verification

| Run | Result |
|---|---|
| Durable 100k (optimized) | 20.0 s = **4,988/s** |
| Durable 500k | **111.5 s = 4,484/s**, 500k committed, 500k accounts — **no superlinear cliff** (stable within 10% of 100k rate; 6.3 GB dev DB unaffected) |
| Durable 1M crash drill (first half 300k before kill) | see §8 |

## 7. Search / browse latency baseline (2.75M-row dev DB, real Django views, logged-in Client, 3 runs median)

Procedure: `python /tmp/search_baseline.py` against `credvault` DB.

```
shape                                best   median  status
dashboard (exact counts)             285ms     307ms  200
url exact                             14ms      15ms  200
url substring (trgm)                  14ms      14ms  200
domain C-collation scan              107ms     117ms  200
ip exact                              31ms      37ms  200
ip prefix (broad)                     32ms      35ms  200
identifier exact                       9ms      11ms  200
all-field keyword                      8ms      10ms  200
browse sort=newest p1                106ms     108ms  200
browse sort=url p1                   106ms     109ms  200
browse sort=url p500 (deep)          104ms     109ms  200
url suggest                            5ms       6ms  200
```

Architecture: reference id pre-resolution via B-tree + GIN trgm, `ID_LIST_LIMIT=5000` subquery vs list heuristic, typed deterministic sorts, `select_related`. All under documented SLA (<1 s for filtered search). No changes made to search path — preserved semantics.

## 8. Crash recovery drill on optimized flush path

**Procedure:**
- 1M-line synthetic file `/tmp/drill_1m.txt` (unique urls/emails).
- Worker `drill_worker.py` → `durable_import_path` with `STREAM_BATCH_SIZE=50k`.
- Poll until `total_committed >= 300k`, then `kill -9 <real python pid>` (not wrapper shell).
- Verify crash state, backdate lease (`now()-2s` to simulate TTL expiry), `recover_job(lines_factory_for_job(job))`.

**Crash state (immediately after SIGKILL at 300k):**
```
job: processing importing worker=6205-*
counters: discovered=1000000 committed=300000 skipped=0 failed=0
checkpoint: {'accounted_through': 300000}
ledger rows: 300000 terminal: 300000 consistent: True max_position: 300000
accounts: 300000 ACCOUNTS==COMMITTED: True
```

**Recovery:**
```
recovering job 35c33913-... status=processing committed=300000 checkpoint=300000
poll 13: processing 550000 ... poll 25: 800000 ... poll 36: completed 1000000
done status=completed committed=1000000 recovery=partial elapsed=187.1s
```

**Final verification:**
```
status=completed discovered=1000000 committed=1000000 skipped=0 failed=0
checkpoint accounted_through=1000000
ledger total=1000000 terminal=1000000
accounts=1000000 duplicate fingerprints=0
positions: min=1 max=1000000 contiguous=True
recovery log: attempt 1 outcome=partial "Resumed 700000 unresolved source line(s); earlier committed outcomes were retained."
EXACTLY-ONCE CHECK: True (committed+skipped+failed == discovered == ledger == accounts == 1M)
```

No lost work, no duplicate accounts, no silent corruption, predictable recovery. The new COPY ledger path preserves atomicity (same transaction as accounts+counters+checkpoint).

## 9. Final acceptance criteria (per directive)

- [x] Resumed from exact repo state without restart — branch/working tree preserved.
- [x] All features audited (COMPLETION_MATRIX.md 18 features) — defects fixed.
- [x] Processing workflows preserve source locations + durable recovery state (source_path/hash/size, per-line hash/position, checkpoint, lease, recovery log).
- [x] Interrupted jobs resume from last valid checkpoint — verified at 300k→1M on optimized path.
- [x] Recovery does not duplicate or lose — exactly-once verified, 0 dup fingerprints.
- [x] Reading/extraction/processing/storage/querying/search measured and optimized per bottlenecks — 49% durable improvement, search baseline recorded.
- [x] Search semantics preserved — all search shapes under 320 ms at 2.75M rows, no incomplete results.
- [x] Regression tests — 199/199 OK, 17/17 perf OK.
- [x] Performance claims reproducible — scripts in `/tmp` + procedure in §3.
- [x] Final state + next action recorded — this file + CONTINUATION.md.

## 10. Files changed in performance phase

- `vault/import_engine.py`: ledger COPY path, remove double normalization, stage-join id resolution.
- `vault/tests_deep_verification.py`: update query-count assertion to match new faster path.
- Docs: `PERFORMANCE_RECOVERY_REPORT.md`, `CONTINUATION.md`.

Stabilization-phase files remain as previously verified (D1–D7).
