# CredVault — Continuation Record (live handoff) — Phase 3 FINAL

> Updated at every milestone. A new session recovers the exact task from this file + repo state.

## Repository state (Phase 3)

- **Path:** `/home/user/credvault`
- **Branch:** `arena/0f03219f-credvault` (base `main` @ `a957402`, Phase 2 @ `befc26c`, Phase 3 current HEAD)
- **Working tree:** Phase 3 optimizations implemented, 199 tests OK, reports updated
  - `vault/import_engine.py`: opt1 skip existing_by_key when provable (checkpoint==attempted and contiguous), opt2 staging-table JOIN for large IN (>20k) with ANALYZE, opt3 fast ledger tuples (no ORM objects) when all existing None, work_mem 1024MB, timing instrumentation
  - `vault/importer.py`: work_mem 1024MB in bulk, maintenance_work_mem 1024MB default
  - Docs: `PERFORMANCE_PHASE3_REPORT.md` (new), `PERFORMANCE_PHASE2_REPORT.md` (baseline), `CONTINUATION.md` (this)
- **Suite:** 199 OK (3 skipped), ~50s; perf 20k: durable ~6,800/s est, bulk ~14k/s; 46 reliability OK, 5 recovery OK
- **Dev DB:** `credvault` empty (truncated after benchmarks)
- **Scratch DB:** `perf_repro` holds 3.25M rows (2.75M bulk 271.7s 10,120/s + 500k durable 138.9s 3,599/s) from Phase 3 populated benchmark — can be truncated: `psql -h 127.0.0.1 -U credvault -d postgres -c "TRUNCATE vault_importrecord, vault_account, vault_url, vault_email, vault_user, vault_pass, vault_ip, vault_importjob CASCADE;" -d perf_repro`

## Environment restore

- `.venv` at repo root (Django 5.2.18, psycopg 3.3.6, Python 3.11) — rebuild: `python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt pgserver`
- PG 16.2 at `/tmp/pgtest/data` — rebuild pg_trgm from GitHub REL_16_2:
  ```
  mkdir -p /tmp/pgtrgm && cd /tmp/pgtrgm
  for f in Makefile trgm.h trgm_op.c trgm_gin.c trgm_gist.c trgm_regexp.c pg_trgm.control pg_trgm--1.0--1.1.sql pg_trgm--1.1--1.2.sql pg_trgm--1.2--1.3.sql pg_trgm--1.3--1.4.sql pg_trgm--1.3.sql pg_trgm--1.4--1.5.sql pg_trgm--1.5--1.6.sql; do curl -s -H "Accept: application/vnd.github.raw" https://api.github.com/repos/postgres/postgres/contents/contrib/pg_trgm/$f?ref=REL_16_2 -o $f; done
  make USE_PGXS=1 PG_CONFIG=/home/user/credvault/.venv/lib/python3.11/site-packages/pgserver/pginstall/bin/pg_config install
  rm -rf /tmp/pgtest/data && /home/user/credvault/.venv/lib/python3.11/site-packages/pgserver/pginstall/bin/initdb -D /tmp/pgtest/data -U credvault --no-locale --encoding=UTF8 --auth=trust
  echo "host all all 127.0.0.1/32 trust" >> /tmp/pgtest/data/pg_hba.conf
  /home/user/credvault/.venv/lib/python3.11/site-packages/pgserver/pginstall/bin/pg_ctl -D /tmp/pgtest/data -l /tmp/pgtest/pg.log -w start
  /home/user/credvault/.venv/lib/python3.11/site-packages/pgserver/pginstall/bin/psql -h 127.0.0.1 -U credvault -d postgres -c "CREATE DATABASE credvault OWNER credvault" -c "CREATE DATABASE perf_repro OWNER credvault"
  ```
- Migrate: `POSTGRES_HOST=127.0.0.1 POSTGRES_USER=credvault POSTGRES_PASSWORD=credvault123 POSTGRES_DB=credvault .venv/bin/python manage.py migrate && POSTGRES_DB=perf_repro ... migrate`
- Superuser: `... manage.py shell -c "from django.contrib.auth import get_user_model; User=get_user_model(); User.objects.create_superuser('admin','admin@example.com','admin12345')"`
- Env vars: `POSTGRES_HOST=127.0.0.1 POSTGRES_USER=credvault POSTGRES_PASSWORD=credvault123 POSTGRES_DB=credvault`
- Perf files: `/tmp/perf_500k.txt` (0-500k), `/tmp/perf_500k_2.txt` (500k-1M), `/tmp/perf_100k.txt` (0-100k), `/tmp/perf_1750k.txt` (1M-2.75M), `/tmp/perf_500k_3.txt` (2.75M-3.25M) — generated via Python script

## Last completed task + verified results

**Phase 3 performance optimizations (befc26c → current):**

1. **Avoid existing_by_key when provable:** Skip IN query when `total_attempted == checkpoint` and batch contiguous starting at checkpoint+1 (common forward case). Proof: checkpoint gap-free prefix, no positions beyond attempted. Saves 0.79s per 100k batch (0.79s → 0.08s). Verified by recovery tests.

2. **Staging-table JOIN for large IN:** For `len(keys) > 20000`, COPY keys to `vault_import_idem_stage` temp + `ANALYZE` + `JOIN ... FOR UPDATE` instead of 100k bind params. Measured: IN 50k 0.741s vs staging 0.244s (3× faster). Threshold 20k avoids slower 10k case (IN 0.171s vs staging 0.305s).

3. **Set-based ledger via tuples (no ORM objects):** Fast path when all `candidate_existing` are None (forward case, no existing ledger rows for these keys). Build fingerprints with local bindings, query existing Accounts only for fps not in newly_created, track first_seen for duplicate within batch, build list of tuples `(job_pk, idem_key, pos, hash, '', status, acc_id, fp, '', cat, False, now)` and direct COPY. Saves 1.57s per 100k (Python loop + 100k model instances) → 10.9s 9,154/s vs 13.2s 7,598/s (+20%) for 100k, 60.9s 8,205/s vs 68.6s 7,290/s (+12.5%) for 500k.

4. **Tune batch/workers/PG:** Batch sweep 25k-250k: 50k 8651/s, 75k 8739/s, 100k 8770/s (optimal), 150k 8457/s. Workers: 1 7612/s, 2 8666/s, 3 8595/s, 4 8516/s — 2 optimal for 2-core. PG: work_mem 512MB→1024MB: 60.9s 8205/s → 57.9s 8637/s (+5%), maintenance_work_mem 512MB→1024MB for faster 31-index rebuild.

**Benchmarks against populated DB (measured, not estimated):**
- Empty 500k fast 100k batch: Phase2 67.1s 7448/s → Phase3 57.9s 8637/s (+16%)
- Second 500k with 500k existing: 86.8s 5758/s → 76.0s 6583/s (+14%)
- 500k with 2.75M existing (measured): 138.9s 3599/s (bulk populate 2.75M 271.7s 10120/s)
- Bulk 500k fast: 32.0s 15632/s → 35s 14000/s (variance, ceiling ~14-15k/s)
- 18M T = N/R: at 3599/s (2.75M existing) 83min, at 6583/s (500k) 45min, at 8637/s (empty) 35min, average 5-6k/s → 50-60min vs target 15min (20,200/s). Target not achievable with exactly-once durable on this hardware.

**Correctness preserved:** 199 OK (3 skipped), 46 reliability OK, 5 recovery OK, source-change detection, uniqueness, idempotency, checkpoints, lease fencing, ledger/account reconciliation, search semantics (31-index fast mode, crash-safe ensure_before_drop, maintenance_work_mem 1GB).

## Current task

**Phase 3 complete.** All requested optimizations implemented, benchmarked, regression tested. Repository at clean documented stopping point per task:

- Do not add new product features or weaken durability — complied (fast ledger still durable, COPY inside transaction, checkpoint same, no removal of ledger)
- Update performance report and continuation record — done (PHASE3 report, CONTINUATION updated)
- Leave at verified stopping point — 199 OK, reports committed, env restore recipe documented

Next optional (needs approval):
- SQL-based ledger via INSERT...SELECT with window function for first occurrence — est +10-15%
- Parallel DB inserts with multiple connections — breaks single-transaction exactly-once, needs 2PC
- PG config: checkpoint_timeout 1h, max_wal_size 10GB, wal_compression

## Known findings

- Fast mode 31 indexes: dropping ledger indexes saves 1.92s→0.44s per 100k ledger COPY (4.3×) but rebuild of 31 indexes for 3.25M rows takes ~20-30s with 1GB maintenance_work_mem.
- existing_by_key skip saves 0.7s per batch but only for forward contiguous case; recovery with gaps still needs lookup (staging path).
- Bulk ceiling ~14-15k/s < 20.2k/s target, durable ceiling ~8.6k/s empty, ~3.6k/s with 2.75M existing — 20.2k/s requires more cores, faster storage, or removing ledger.
- Timing instrumentation `VAULT_DEBUG_TIMING=1` adds ~0.1s per batch, kept behind env var.

## Next exact action

**None required** — Phase 3 meets task criteria. If new work starts, begin from this file + `PERFORMANCE_PHASE3_REPORT.md` + repo state at current HEAD. To clean scratch DB: `psql -h 127.0.0.1 -U credvault -d postgres -c "DROP DATABASE perf_repro;"` or truncate as above. To clean /tmp: `rm /tmp/perf_*.txt /tmp/pgtrgm -rf`.
